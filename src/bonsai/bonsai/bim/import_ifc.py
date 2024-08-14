# Bonsai - OpenBIM Blender Add-on
# Copyright (C) 2020, 2021 Dion Moult <dion@thinkmoult.com>
#
# This file is part of Bonsai.
#
# Bonsai is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Bonsai is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Bonsai.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations
import bpy
import time
import json
import logging
import mathutils
import numpy as np
import multiprocessing
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.unit
import ifcopenshell.util.element
import ifcopenshell.util.geolocation
import ifcopenshell.util.placement
import ifcopenshell.util.representation
import ifcopenshell.util.shape
import bonsai.tool as tool
from itertools import chain, accumulate
from bonsai.bim.ifc import IfcStore, IFC_CONNECTED_TYPE
from bonsai.tool.loader import OBJECT_DATA_TYPE
from typing import Dict, Union, Optional, Any


class MaterialCreator:
    def __init__(self, ifc_import_settings: IfcImportSettings, ifc_importer: IfcImporter):
        self.mesh: bpy.types.Mesh = None
        self.obj: bpy.types.Object = None
        self.styles: Dict[int, bpy.types.Material] = {}
        self.parsed_meshes: set[str] = set()
        self.ifc_import_settings = ifc_import_settings
        self.ifc_importer = ifc_importer

    def create(self, element: ifcopenshell.entity_instance, obj: bpy.types.Object, mesh: OBJECT_DATA_TYPE) -> None:
        self.mesh = mesh
        # as ifcopenshell triangulates the mesh, we need to merge it to quads again
        self.obj = obj
        if (hasattr(element, "Representation") and not element.Representation) or (
            hasattr(element, "RepresentationMaps") and not element.RepresentationMaps
        ):
            return
        if not self.mesh or self.mesh.name in self.parsed_meshes:
            return

        # We don't support curve styles yet.
        if isinstance(mesh, bpy.types.Curve):
            return

        # mesh["ios_materials"] can contain:
        # - ifc style id if style assigned to the representation items directly
        # or through material with a style;
        # - ifc material id if both true:
        #   - element has a material without a style;
        #   - there are parts of the geometry that has no other style assigned to them;
        # - -1 in case if there is no material;
        # - 0 in case if there are default materials used.
        # Though 0 value will not occur as we don't use default materials in IfcImporter.

        self.parsed_meshes.add(self.mesh.name)
        self.load_texture_maps()
        self.assign_material_slots_to_faces()
        tool.Geometry.record_object_materials(obj)
        del self.mesh["ios_materials"]

    def load_existing_materials(self) -> None:
        for material in bpy.data.materials:
            if ifc_definition_id := material.BIMStyleProperties.ifc_definition_id:
                self.styles[ifc_definition_id] = material

    def get_ifc_coordinate(self, material: bpy.types.Material) -> Union[ifcopenshell.entity_instance, None]:
        """Get IfcTextureCoordinate"""
        texture_style = tool.Style.get_texture_style(material)
        if not texture_style:
            return
        for texture in texture_style.Textures or []:
            if coords := getattr(texture, "IsMappedBy", None):
                coords = coords[0]
                # IfcTextureCoordinateGenerator handled in the style shader graph
                if coords.is_a("IfcIndexedTextureMap"):
                    return coords
                # TODO: support IfcTextureMap
                if coords.is_a("IfcTextureMap"):
                    print(f"WARNING. IfcTextureMap texture coordinates is not supported.")
                    return

    def load_texture_maps(self) -> None:
        for style_or_material_id in self.mesh["ios_materials"]:
            if not (material := self.styles.get(style_or_material_id)):
                continue

            material = self.styles[style_or_material_id]
            if coords := self.get_ifc_coordinate(material):
                tool.Loader.load_indexed_texture_map(coords, self.mesh)

    def assign_material_slots_to_faces(self) -> None:
        if not self.mesh["ios_materials"]:
            return

        ios_materials = self.mesh["ios_materials"]
        if len(ios_materials) == 1:
            style_or_material_id = ios_materials[0]
            # Has no styles / has just a material without a style.
            if not (material := self.styles.get(style_or_material_id)):
                return
            # Has a style and it's assigned to the entire geometry.
            # Otherwise we'll need to proceed as faces without styles
            # will require an empty material slot.
            if -1 not in self.mesh["ios_material_ids"]:
                self.mesh.materials.append(material)
                return

        # Mapping of ios_materials indices
        # to blender material slots indices.
        material_to_slot: dict[int, int] = {}
        empty_slot_index = None

        def get_empty_slot_index() -> int:
            nonlocal empty_slot_index
            if empty_slot_index is None:
                self.mesh.materials.append(None)
                empty_slot_index = len(self.mesh.materials) - 1
            return empty_slot_index

        # TODO: When they are not equal?
        if len(self.mesh.polygons) == len(self.mesh["ios_material_ids"]):
            for i, style_or_material_id in enumerate(ios_materials):
                material_without_style = style_or_material_id not in self.styles
                if material_without_style:
                    slot_index = get_empty_slot_index()
                else:
                    blender_material = self.styles[style_or_material_id]
                    self.mesh.materials.append(blender_material)
                    slot_index = len(self.mesh.materials) - 1
                material_to_slot[i] = slot_index

            if -1 in self.mesh["ios_material_ids"]:
                material_to_slot[-1] = get_empty_slot_index()

            material_index = [material_to_slot[mat_id] for mat_id in self.mesh["ios_material_ids"]]
            self.mesh.polygons.foreach_set("material_index", material_index)

    def resolve_all_stylable_representation_items(
        self, representation: ifcopenshell.entity_instance
    ) -> list[ifcopenshell.entity_instance]:
        """returns list of resolved IfcRepresentationItems"""
        items = []
        for item in representation.Items:
            if item.is_a("IfcMappedItem"):
                items.extend(item.MappingSource.MappedRepresentation.Items)
            if item.is_a("IfcBooleanResult"):
                operand = item.FirstOperand
                while True:
                    items.append(operand)
                    if operand.is_a("IfcBooleanResult"):
                        operand = operand.FirstOperand
                    else:
                        break
            items.append(item)
        return items


class IfcImporter:
    def __init__(self, ifc_import_settings: IfcImportSettings):
        self.ifc_import_settings = ifc_import_settings
        tool.Loader.set_settings(ifc_import_settings)
        self.diff = None
        self.file: ifcopenshell.file = None
        self.project = None
        self.has_existing_project = False
        # element guids to blender collections mapping
        self.collections: dict[str, bpy.types.Collection] = {}
        self.elements: set[ifcopenshell.entity_instance] = set()
        self.annotations: set[ifcopenshell.entity_instance] = set()
        self.gross_elements: set[ifcopenshell.entity_instance] = set()
        self.element_types: set[ifcopenshell.entity_instance] = set()
        self.spatial_elements: set[ifcopenshell.entity_instance] = set()
        self.type_products = {}
        self.meshes: dict[str, OBJECT_DATA_TYPE] = {}
        self.mesh_shapes = {}
        self.time = 0
        self.unit_scale = 1
        # ifc definition ids to blender elements mapping
        self.added_data: dict[int, IFC_CONNECTED_TYPE] = {}
        self.native_elements = set()
        self.native_data = {}
        self.progress = 0

        self.material_creator = MaterialCreator(ifc_import_settings, self)

    def profile_code(self, message: str) -> None:
        if not self.time:
            self.time = time.time()
        print("{} :: {:.2f}".format(message, time.time() - self.time))
        self.time = time.time()
        self.update_progress(self.progress + 1)

    def update_progress(self, progress: float) -> None:
        if progress <= 100:
            self.progress = progress
        bpy.context.window_manager.progress_update(self.progress)

    def execute(self) -> None:
        bpy.context.window_manager.progress_begin(0, 100)
        self.profile_code("Starting import process")
        self.load_file()
        self.profile_code("Loading file")
        self.calculate_unit_scale()
        self.profile_code("Calculate unit scale")
        self.process_context_filter()
        self.profile_code("Process context filter")
        self.calculate_model_offset()
        self.profile_code("Calculate model offset")
        self.predict_dense_mesh()
        self.profile_code("Predict dense mesh")
        self.set_units()
        self.profile_code("Set units")
        self.create_project()
        self.profile_code("Create project")
        self.process_element_filter()
        self.profile_code("Process element filter")
        self.create_styles()
        self.profile_code("Create styles")
        self.parse_native_elements()
        self.profile_code("Parsing native elements")
        self.create_native_elements()
        self.profile_code("Create native elements")
        self.create_elements()
        self.profile_code("Create elements")
        self.create_generic_elements(self.annotations)
        self.profile_code("Create annotations")
        self.create_positioning_elements()
        self.profile_code("Create positioning elements")
        self.create_spatial_elements()
        self.profile_code("Create spatial elements")
        self.create_structural_items()
        self.profile_code("Create structural items")
        self.create_element_types()
        self.profile_code("Create element types")
        self.place_objects_in_collections()
        self.profile_code("Place objects in collections")
        self.add_project_to_scene()
        self.profile_code("Add project to scene")
        if self.ifc_import_settings.should_clean_mesh and len(self.file.by_type("IfcElement")) < 1000:
            self.clean_mesh()
            self.profile_code("Mesh cleaning")
        if self.ifc_import_settings.should_merge_materials_by_colour:
            self.merge_materials_by_colour()
            self.profile_code("Merging by colour")
        self.set_default_context()
        self.profile_code("Setting default context")
        if self.ifc_import_settings.should_setup_viewport_camera:
            self.setup_viewport_camera()
        self.setup_arrays()
        self.profile_code("Setup arrays")
        tool.Spatial.run_spatial_import_spatial_decomposition()
        if default_container := tool.Spatial.guess_default_container():
            tool.Spatial.set_default_container(default_container)
        self.update_progress(100)
        bpy.context.window_manager.progress_end()

    def process_context_filter(self) -> None:
        tool.Loader.settings.contexts = ifcopenshell.util.representation.get_prioritised_contexts(self.file)
        tool.Loader.settings.context_settings = tool.Loader.create_settings()
        tool.Loader.settings.gross_context_settings = tool.Loader.create_settings(is_gross=True)

    def process_element_filter(self) -> None:
        offset = self.ifc_import_settings.element_offset
        offset_limit = offset + self.ifc_import_settings.element_limit

        if self.ifc_import_settings.has_filter:
            self.elements = self.ifc_import_settings.elements
            if isinstance(self.elements, set):
                self.elements = list(self.elements)
            # TODO: enable filtering for annotations
        else:
            if self.file.schema in ("IFC2X3", "IFC4"):
                self.elements = self.file.by_type("IfcElement") + self.file.by_type("IfcProxy")
            else:
                self.elements = self.file.by_type("IfcElement")

        drawing_groups = [g for g in self.file.by_type("IfcGroup") if g.ObjectType == "DRAWING"]
        drawing_annotations = set()
        for drawing_group in drawing_groups:
            for rel in drawing_group.IsGroupedBy:
                drawing_annotations.update(rel.RelatedObjects)
        self.annotations = set([a for a in self.file.by_type("IfcAnnotation")])
        self.annotations -= drawing_annotations

        self.elements = [e for e in self.elements if not e.is_a("IfcFeatureElement") or e.is_a("IfcSurfaceFeature")]
        self.elements = set(self.elements[offset:offset_limit])

        if self.ifc_import_settings.has_filter or offset or offset_limit < len(self.elements):
            self.element_types = set([ifcopenshell.util.element.get_type(e) for e in self.elements])
        else:
            self.element_types = set(self.file.by_type("IfcTypeProduct"))

        if self.ifc_import_settings.has_filter and self.ifc_import_settings.should_filter_spatial_elements:
            filtered_elements = self.elements | set(self.file.by_type("IfcGrid"))
            self.spatial_elements = self.get_spatial_elements_filtered_by_elements(filtered_elements)
        else:
            if self.file.schema == "IFC2X3":
                self.spatial_elements = set(self.file.by_type("IfcSpatialStructureElement"))
            else:
                self.spatial_elements = set(self.file.by_type("IfcSpatialElement"))

        # Detect excessive voids
        self.gross_elements = set(
            filter(lambda e: len(getattr(e, "HasOpenings", [])) > self.ifc_import_settings.void_limit, self.elements)
        )
        self.elements = self.elements.difference(self.gross_elements)

        if self.gross_elements:
            print("Warning! Excessive voids were found and skipped for the following elements:")
            for element in self.gross_elements:
                print(element)

    def get_spatial_elements_filtered_by_elements(
        self, elements: set[ifcopenshell.entity_instance]
    ) -> set[ifcopenshell.entity_instance]:
        leaf_spatial_elements = set([ifcopenshell.util.element.get_container(e) for e in elements])
        results = set()
        for spatial_element in leaf_spatial_elements:
            while True:
                results.add(spatial_element)
                spatial_element = ifcopenshell.util.element.get_aggregate(spatial_element)
                if not spatial_element or spatial_element.is_a() in ("IfcProject", "IfcProjectLibrary"):
                    break
        return results

    def parse_native_elements(self) -> None:
        if not self.ifc_import_settings.should_load_geometry:
            return
        for element in self.elements:
            if self.is_native(element):
                self.native_elements.add(element)
        self.elements -= self.native_elements

    def is_native(self, element: ifcopenshell.entity_instance) -> bool:
        if (
            not element.Representation
            or not element.Representation.Representations
            or getattr(element, "HasOpenings", None)
        ):
            return False

        representation = None
        representation_priority = None
        context = None

        for rep in element.Representation.Representations:
            if rep.ContextOfItems in tool.Loader.settings.contexts:
                rep_priority = tool.Loader.settings.contexts.index(rep.ContextOfItems)
                if representation is None or rep_priority < representation_priority:
                    representation = rep
                    representation_priority = rep_priority
                    context = rep.ContextOfItems

        if not representation:
            return False

        matrix = np.eye(4)
        representation_id = None

        rep = representation
        while True:
            if len(rep.Items) == 1 and rep.Items[0].is_a("IfcMappedItem"):
                rep_matrix = ifcopenshell.util.placement.get_mappeditem_transformation(rep.Items[0])
                if not np.allclose(rep_matrix, np.eye(4)):
                    matrix = rep_matrix @ matrix
                    if representation_id is None:
                        representation_id = rep.id()
                rep = rep.Items[0].MappingSource.MappedRepresentation
                if not rep:  # Accommodate invalid files
                    return False
            else:
                if representation_id is None:
                    representation_id = rep.id()
                break
        resolved_representation = ifcopenshell.util.representation.resolve_representation(representation)

        matrix[0][3] *= self.unit_scale
        matrix[1][3] *= self.unit_scale
        matrix[2][3] *= self.unit_scale

        # Single swept disk solids (e.g. rebar) are better natively represented as beveled curves
        if self.is_native_swept_disk_solid(element, resolved_representation):
            self.native_data[element.GlobalId] = {
                "matrix": matrix,
                "context": context,
                "geometry_id": representation_id,
                "representation": resolved_representation,
                "type": "IfcSweptDiskSolid",
            }
            return True

        if not self.ifc_import_settings.should_use_native_meshes:
            return False  # Performance improvements only occur on edge cases currently

        # FacetedBreps (without voids) are meshes. See #841.
        if self.is_native_faceted_brep(resolved_representation):
            self.native_data[element.GlobalId] = {
                "matrix": matrix,
                "context": context,
                "geometry_id": representation_id,
                "representation": resolved_representation,
                "type": "IfcFacetedBrep",
            }
            return True

        if self.is_native_face_based_surface_model(resolved_representation):
            self.native_data[element.GlobalId] = {
                "matrix": matrix,
                "context": context,
                "geometry_id": representation_id,
                "representation": resolved_representation,
                "type": "IfcFaceBasedSurfaceModel",
            }
            return True
        return False

    def is_native_swept_disk_solid(
        self, element: ifcopenshell.entity_instance, representation: ifcopenshell.entity_instance
    ) -> bool:
        items = [i["item"] for i in ifcopenshell.util.representation.resolve_items(representation)]
        if len(items) == 1 and items[0].is_a("IfcSweptDiskSolid"):
            if tool.Blender.Modifier.is_railing(element):
                return False
            return True
        elif len(items) and (  # See #2508 why we accommodate for invalid IFCs here
            items[0].is_a("IfcSweptDiskSolid")
            and len({i.is_a() for i in items}) == 1
            and len({i.Radius for i in items}) == 1
        ):
            if tool.Blender.Modifier.is_railing(element):
                return False
            return True
        return False

    def is_native_faceted_brep(self, representation: ifcopenshell.entity_instance) -> bool:
        # TODO handle mapped items
        for i in representation.Items:
            if i.is_a() != "IfcFacetedBrep":
                return False
        return True

    def is_native_face_based_surface_model(self, representation: ifcopenshell.entity_instance) -> bool:
        for i in representation.Items:
            if i.is_a() != "IfcFaceBasedSurfaceModel":
                return False
        return True

    def get_products_from_shape_representation(self, element: ifcopenshell.entity_instance) -> None:
        products = [pr.ShapeOfProduct[0] for pr in element.OfProductRepresentation]
        for rep_map in element.RepresentationMap:
            for usage in rep_map.MapUsage:
                for inverse_element in self.file.get_inverse(usage):
                    if inverse_element.is_a("IfcShapeRepresentation"):
                        products.extend(self.get_products_from_shape_representation(inverse_element))
        return products

    def predict_dense_mesh(self) -> None:
        if self.ifc_import_settings.should_use_native_meshes:
            return

        threshold = 10000  # Just from experience.

        # The check for CfsFaces/Faces/CoordIndex accommodates invalid data from Cadwork
        # 0 IfcClosedShell.CfsFaces
        faces = [len(faces) for e in self.file.by_type("IfcClosedShell") if (faces := e[0])]
        if faces and max(faces) > threshold:
            self.ifc_import_settings.should_use_native_meshes = True
            return

        if self.file.schema == "IFC2X3":
            return

        # 2 IfcPolygonalFaceSet.Faces
        faces = [len(faces) for e in self.file.by_type("IfcPolygonalFaceSet") if (faces := e[2])]
        if faces and max(faces) > threshold:
            self.ifc_import_settings.should_use_native_meshes = True
            return

        # 3 IfcTriangulatedFaceSet.CoordIndex
        faces = [len(index) for e in self.file.by_type("IfcTriangulatedFaceSet") if (index := e[3])]
        if faces and max(faces) > threshold:
            self.ifc_import_settings.should_use_native_meshes = True

    def calculate_model_offset(self) -> None:
        props = bpy.context.scene.BIMGeoreferenceProperties
        if self.ifc_import_settings.false_origin_mode == "MANUAL":
            tool.Loader.set_manual_blender_offset(self.file)
        elif self.ifc_import_settings.false_origin_mode == "AUTOMATIC":
            if not props.has_blender_offset:
                tool.Loader.guess_false_origin(self.file)
        tool.Georeference.set_model_origin()

    def create_positioning_elements(self):
        self.create_grids()
        self.create_alignments()

    def create_alignments(self):
        if not self.ifc_import_settings.should_load_geometry:
            return
        if self.file.schema in ("IFC2X3", "IFC4"):
            return
        self.create_generic_elements(set(self.file.by_type("IfcLinearPositioningElement")))
        self.create_generic_elements(set(self.file.by_type("IfcReferent")))
        # Loading IfcLinearElement for test purposes for now only.
        # In the future we will make it lazy loaded and toggleable with a special UI.
        # self.create_generic_elements(set(self.file.by_type("IfcLinearElement")))

    def create_grids(self):
        if not self.ifc_import_settings.should_load_geometry:
            return
        for grid in self.file.by_type("IfcGrid"):
            shape = None
            if not grid.UAxes or not grid.VAxes:
                # Revit can create invalid grids
                self.ifc_import_settings.logger.error("An invalid grid was found %s", grid)
                continue
            if grid.Representation:
                shape = tool.Loader.create_generic_shape(grid)
            grid_obj = self.create_product(grid, shape)
            grid_placement = self.get_element_matrix(grid)
            if tool.Blender.get_addon_preferences().lock_grids_on_import:
                grid_obj.lock_location = (True, True, True)
                grid_obj.lock_rotation = (True, True, True)
            self.create_grid_axes(grid.UAxes, grid_obj, grid_placement)
            self.create_grid_axes(grid.VAxes, grid_obj, grid_placement)
            if grid.WAxes:
                self.create_grid_axes(grid.WAxes, grid_obj, grid_placement)

    def create_grid_axes(self, axes, grid_obj, grid_placement):
        for axis in axes:
            shape = tool.Loader.create_generic_shape(axis.AxisCurve)
            mesh = self.create_mesh(axis, shape)
            obj = bpy.data.objects.new(tool.Loader.get_name(axis), mesh)
            if tool.Blender.get_addon_preferences().lock_grids_on_import:
                obj.lock_location = (True, True, True)
                obj.lock_rotation = (True, True, True)
            self.link_element(axis, obj)
            self.set_matrix_world(obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, grid_placement.copy()))

    def create_element_types(self):
        for element_type in self.element_types:
            if not element_type:
                continue
            self.create_element_type(element_type)

    def create_element_type(self, element: ifcopenshell.entity_instance) -> None:
        self.ifc_import_settings.logger.info("Creating object %s", element)
        mesh = None
        if self.ifc_import_settings.should_load_geometry:
            for context in tool.Loader.settings.contexts:
                representation = ifcopenshell.util.representation.get_representation(element, context)
                if not representation:
                    continue
                mesh_name = "{}/{}".format(representation.ContextOfItems.id(), representation.id())
                mesh = self.meshes.get(mesh_name)
                if mesh is None:
                    shape = tool.Loader.create_generic_shape(representation)
                    if shape:
                        mesh = self.create_mesh(element, shape)
                        tool.Loader.link_mesh(shape, mesh)
                        self.meshes[mesh_name] = mesh
                    else:
                        self.ifc_import_settings.logger.error("Failed to generate shape for %s", element)
                break
        obj = bpy.data.objects.new(tool.Loader.get_name(element), mesh)
        self.link_element(element, obj)
        self.material_creator.create(element, obj, mesh)
        self.type_products[element.GlobalId] = obj

    def create_native_elements(self):
        if not self.ifc_import_settings.should_load_geometry:
            return
        progress = 0
        checkpoint = time.time()
        total = len(self.native_elements)
        for element in self.native_elements:
            progress += 1
            if progress % 250 == 0:
                percent = round(progress / total * 100)
                print(
                    "{} / {} ({}%) elements processed in {:.2f}s ...".format(
                        progress, total, percent, time.time() - checkpoint
                    )
                )
                checkpoint = time.time()
            native_data = self.native_data[element.GlobalId]
            mesh_name = f"{native_data['context'].id()}/{native_data['geometry_id']}"
            mesh = self.meshes.get(mesh_name)
            if mesh is None:
                if native_data["type"] == "IfcSweptDiskSolid":
                    mesh = self.create_native_swept_disk_solid(element, mesh_name, native_data)
                elif native_data["type"] == "IfcFacetedBrep":
                    mesh = self.create_native_faceted_brep(element, mesh_name, native_data)
                elif native_data["type"] == "IfcFaceBasedSurfaceModel":
                    mesh = self.create_native_faceted_brep(element, mesh_name, native_data)
                tool.Ifc.link(tool.Ifc.get().by_id(native_data["geometry_id"]), mesh)
                mesh.name = mesh_name
                self.meshes[mesh_name] = mesh
            self.create_product(element, mesh=mesh)
        print("Done creating geometry")

    def create_spatial_elements(self) -> None:
        if tool.Blender.get_addon_preferences().spatial_elements_unselectable:
            self.create_generic_elements(self.spatial_elements, unselectable=True)
        else:
            self.create_generic_elements(self.spatial_elements, unselectable=False)

    def create_elements(self) -> None:
        self.create_generic_elements(self.elements)
        self.create_generic_elements(self.gross_elements, is_gross=True)

    def create_generic_elements(
        self, elements: set[ifcopenshell.entity_instance], unselectable=False, is_gross=False
    ) -> None:
        if isinstance(self.file, ifcopenshell.sqlite):
            return self.create_generic_sqlite_elements(elements)

        if self.ifc_import_settings.should_load_geometry:
            context_settings = (
                tool.Loader.settings.gross_context_settings if is_gross else tool.Loader.settings.context_settings
            )
            for settings in context_settings:
                if not elements:
                    break
                products = self.create_products(elements, settings=settings)
                elements -= products
            products = self.create_pointclouds(elements)
            elements -= products

        total = len(elements)
        objects = set()
        for i, element in enumerate(elements):
            if i % 250 == 0:
                print("{} / {} elements processed ...".format(i, total))
            objects.add(self.create_product(element))

        if unselectable:
            for obj in objects:
                obj.hide_select = True

    def create_generic_sqlite_elements(self, elements: set[ifcopenshell.entity_instance]) -> None:
        self.geometry_cache = self.file.get_geometry([e.id() for e in elements])
        for geometry_id, geometry in self.geometry_cache["geometry"].items():
            mesh_name = tool.Loader.get_mesh_name_from_shape(type("Geometry", (), {"id": geometry_id}))
            mesh = bpy.data.meshes.new(mesh_name)

            verts = geometry["verts"]
            mesh["has_cartesian_point_offset"] = False

            if geometry["faces"]:
                num_vertices = len(verts) // 3
                total_faces = len(geometry["faces"])
                loop_start = range(0, total_faces, 3)
                num_loops = total_faces // 3
                loop_total = [3] * num_loops
                num_vertex_indices = len(geometry["faces"])

                mesh.vertices.add(num_vertices)
                mesh.vertices.foreach_set("co", verts)
                mesh.loops.add(num_vertex_indices)
                mesh.loops.foreach_set("vertex_index", geometry["faces"])
                mesh.polygons.add(num_loops)
                mesh.polygons.foreach_set("loop_start", loop_start)
                mesh.polygons.foreach_set("loop_total", loop_total)
                mesh.update()
            else:
                e = geometry["edges"]
                v = verts
                vertices = [[v[i], v[i + 1], v[i + 2]] for i in range(0, len(v), 3)]
                edges = [[e[i], e[i + 1]] for i in range(0, len(e), 2)]
                mesh.from_pydata(vertices, edges, [])

            mesh["ios_materials"] = geometry["materials"]
            mesh["ios_material_ids"] = geometry["material_ids"]
            self.meshes[mesh_name] = mesh

        total = len(elements)
        for i, element in enumerate(elements):
            if i % 250 == 0:
                print("{} / {} elements processed ...".format(i, total))
            mesh = None
            geometry_id = self.geometry_cache["shapes"][element.id()]["geometry"]
            if geometry_id:
                mesh_name = tool.Loader.get_mesh_name_from_shape(type("Geometry", (), {"id": geometry_id}))
                mesh = self.meshes.get(mesh_name)
            self.create_product(element, mesh=mesh)

    def create_products(
        self,
        products: set[ifcopenshell.entity_instance],
        settings: Optional[ifcopenshell.geom.main.settings] = None,
    ) -> set[ifcopenshell.entity_instance]:
        results = set()
        if not products:
            return results
        if tool.Loader.settings.should_use_cpu_multiprocessing:
            iterator = ifcopenshell.geom.iterator(settings, self.file, multiprocessing.cpu_count(), include=products)
        else:
            iterator = ifcopenshell.geom.iterator(settings, self.file, include=products)
        if self.ifc_import_settings.should_cache:
            cache = IfcStore.get_cache()
            if cache:
                iterator.set_cache(cache)
        valid_file = iterator.initialize()
        if not valid_file:
            return results
        checkpoint = time.time()
        progress = 0
        total = len(products)
        start_progress = self.progress
        progress_range = 85 - start_progress
        while True:
            progress += 1
            if progress % 250 == 0:
                percent_created = round(progress / total * 100)
                percent_preprocessed = iterator.progress()
                percent_average = (percent_created + percent_preprocessed) / 2
                print(
                    "{} / {} ({}% created, {}% preprocessed) elements processed in {:.2f}s ...".format(
                        progress, total, percent_created, percent_preprocessed, time.time() - checkpoint
                    )
                )
                checkpoint = time.time()
                self.update_progress((percent_average / 100 * progress_range) + start_progress)
            shape = iterator.get()
            if shape:
                product = self.file.by_id(shape.id)
                self.create_product(product, shape)
                results.add(product)
            if not iterator.next():
                break
        print("Done creating geometry")
        return results

    def create_structural_items(self):
        self.create_generic_elements(set(self.file.by_type("IfcStructuralCurveMember")))
        self.create_generic_elements(set(self.file.by_type("IfcStructuralCurveConnection")))
        self.create_generic_elements(set(self.file.by_type("IfcStructuralSurfaceMember")))
        self.create_generic_elements(set(self.file.by_type("IfcStructuralSurfaceConnection")))
        self.create_structural_point_connections()

    def create_structural_point_connections(self):
        for product in self.file.by_type("IfcStructuralPointConnection"):
            # TODO: make this based off ifcopenshell. See #1409
            placement_matrix = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
            vertex = None
            context = None
            representation = None
            for subelement in self.file.traverse(product.Representation):
                if subelement.is_a("IfcVertex") and subelement.VertexGeometry.is_a("IfcCartesianPoint"):
                    vertex = list(subelement.VertexGeometry.Coordinates)
                elif subelement.is_a("IfcGeometricRepresentationContext"):
                    context = subelement
                elif subelement.is_a("IfcTopologyRepresentation"):
                    representation = subelement
            if not vertex or not context or not representation:
                continue  # TODO implement non cartesian point vertexes

            mesh_name = tool.Geometry.get_representation_name(representation)
            mesh = bpy.data.meshes.new(mesh_name)
            mesh.from_pydata([mathutils.Vector(vertex) * self.unit_scale], [], [])

            obj = bpy.data.objects.new(tool.Loader.get_name(product), mesh)
            self.set_matrix_world(obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, placement_matrix))
            self.link_element(product, obj)

    def get_pointcloud_representation(self, product):
        if hasattr(product, "Representation") and hasattr(product.Representation, "Representations"):
            representations = product.Representation.Representations
        elif hasattr(product, "RepresentationMaps") and hasattr(product.RepresentationMaps, "RepresentationMaps"):
            representations = product.RepresentationMaps
        else:
            return None

        for representation in representations:
            if representation.RepresentationType in ("PointCloud", "Point"):
                return representation

            elif self.file.schema == "IFC2X3" and representation.RepresentationType == "GeometricSet":
                for item in representation.Items:
                    if not (item.is_a("IfcCartesianPointList") or item.is_a("IfcCartesianPoint")):
                        break
                else:
                    return representation

            elif representation.RepresentationType == "MappedRepresentation":
                for item in representation.Items:
                    mapped_representation = self.get_pointcloud_representation(item)
                    if mapped_representation is not None:
                        return mapped_representation
        return None

    def create_pointclouds(self, products: set[ifcopenshell.entity_instance]) -> set[ifcopenshell.entity_instance]:
        result = set()
        for product in products:
            representation = self.get_pointcloud_representation(product)
            if representation is not None:
                pointcloud = self.create_pointcloud(product, representation)
                if pointcloud is not None:
                    result.add(pointcloud)

        return result

    def create_pointcloud(
        self, product: ifcopenshell.entity_instance, representation: ifcopenshell.entity_instance
    ) -> Union[ifcopenshell.entity_instance, None]:
        placement_matrix = self.get_element_matrix(product)
        vertex_list = []
        for item in representation.Items:
            if item.is_a("IfcCartesianPointList3D"):
                vertex_list.extend(
                    mathutils.Vector(list(coordinates)) * self.unit_scale for coordinates in item.CoordList
                )
            elif item.is_a("IfcCartesianPointList2D"):
                vertex_list.extend(
                    mathutils.Vector(list(coordinates)).to_3d() * self.unit_scale for coordinates in item.CoordList
                )
            elif item.is_a("IfcCartesianPoint"):
                vertex_list.append(mathutils.Vector(list(item.Coordinates)) * self.unit_scale)

        if len(vertex_list) == 0:
            return None

        mesh_name = tool.Geometry.get_representation_name(representation)
        mesh = bpy.data.meshes.new(mesh_name)
        mesh.from_pydata(vertex_list, [], [])
        tool.Ifc.link(representation, mesh)

        obj = bpy.data.objects.new(tool.Loader.get_name(product), mesh)
        self.set_matrix_world(obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, placement_matrix))
        self.link_element(product, obj)
        return product

    def create_product(
        self,
        element: ifcopenshell.entity_instance,
        shape: Optional[Any] = None,
        mesh: Optional[OBJECT_DATA_TYPE] = None,
    ) -> Union[bpy.types.Object, None]:
        if element is None:
            return

        if self.has_existing_project:
            obj = tool.Ifc.get_object(element)
            if obj:
                return obj

        self.ifc_import_settings.logger.info("Creating object %s", element)

        if mesh:
            pass
        elif element.is_a("IfcAnnotation") and self.is_curve_annotation(element) and shape:
            mesh = self.create_curve(element, shape)
            tool.Loader.link_mesh(shape, mesh)
        elif shape:
            mesh_name = tool.Loader.get_mesh_name_from_shape(shape.geometry)
            mesh = self.meshes.get(mesh_name)
            if mesh is None:
                mesh = self.create_mesh(element, shape)
                tool.Loader.link_mesh(shape, mesh)
                self.meshes[mesh_name] = mesh
        else:
            mesh = None

        obj = bpy.data.objects.new(tool.Loader.get_name(element), mesh)
        self.link_element(element, obj)

        if shape:
            # We use numpy here because Blender mathutils.Matrix is not accurate enough
            mat = np.array(shape.transformation.matrix).reshape((4, 4), order="F")
            self.set_matrix_world(obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, mat))
            assert mesh  # Type checker.
            self.material_creator.create(element, obj, mesh)
        elif mesh:
            self.set_matrix_world(
                obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, self.get_element_matrix(element))
            )
            self.material_creator.create(element, obj, mesh)
        elif hasattr(element, "ObjectPlacement"):
            self.set_matrix_world(
                obj, tool.Loader.apply_blender_offset_to_matrix_world(obj, self.get_element_matrix(element))
            )

        return obj

    def load_existing_meshes(self) -> None:
        self.meshes.update({m.name: m for m in bpy.data.meshes})

    def get_representation_item_material_name(self, item):
        if not item.StyledByItem:
            return
        styles = list(item.StyledByItem[0].Styles)
        while styles:
            style = styles.pop()
            if style.is_a("IfcSurfaceStyle"):
                return style.id()
            elif style.is_a("IfcPresentationStyleAssignment"):
                styles.extend(style.Styles)

    def create_native_faceted_brep(self, element, mesh_name, native_data):
        # co [x y z x y z x y z ...]
        # vertex_index [i i i i i ...]
        # loop_start [0 3 6 9 ...] (for tris)
        # loop_total [3 3 3 3 ...] (for tris)
        self.mesh_data = {
            "co": [],
            "vertex_index": [],
            "loop_start": [],
            "loop_total": [],
            "total_verts": 0,
            "total_polygons": 0,
            "materials": [],
            "material_ids": [],
        }

        for item in native_data["representation"].Items:
            if item.is_a() == "IfcFacetedBrep":
                self.convert_representation_item_faceted_brep(item)
            elif item.is_a() == "IfcFaceBasedSurfaceModel":
                self.convert_representation_item_face_based_surface_model(item)

        mesh = bpy.data.meshes.new("Native")

        props = bpy.context.scene.BIMGeoreferenceProperties
        if props.has_blender_offset and tool.Loader.is_point_far_away(self.mesh_data["co"][0:3], is_meters=False):
            verts_array = np.array(self.mesh_data["co"])
            verts_array *= self.unit_scale
            offset_x, offset_y, offset_z = verts_array[0:3]
            offset = np.array([-offset_x, -offset_y, -offset_z])
            offset_verts = verts_array + np.tile(offset, len(verts_array) // 3)

            if np.allclose(native_data["matrix"], np.identity(4), atol=1e-8):
                verts = offset_verts.tolist()
            else:
                verts = self.apply_matrix_to_flat_coords(offset_verts, native_data["matrix"])

            mesh["has_cartesian_point_offset"] = True
            mesh["cartesian_point_offset"] = f"{offset_x},{offset_y},{offset_z}"
        else:
            verts_array = np.array(self.mesh_data["co"])
            verts_array *= self.unit_scale
            if np.allclose(native_data["matrix"], np.identity(4), atol=1e-8):
                verts = verts_array.tolist()
            else:
                verts = self.apply_matrix_to_flat_coords(verts_array, native_data["matrix"])
            mesh["has_cartesian_point_offset"] = False

        mesh.vertices.add(self.mesh_data["total_verts"])
        mesh.vertices.foreach_set("co", verts)
        mesh.loops.add(len(self.mesh_data["vertex_index"]))
        mesh.loops.foreach_set("vertex_index", self.mesh_data["vertex_index"])
        mesh.polygons.add(self.mesh_data["total_polygons"])
        mesh.polygons.foreach_set("loop_start", self.mesh_data["loop_start"])
        mesh.polygons.foreach_set("loop_total", self.mesh_data["loop_total"])
        mesh.polygons.foreach_set("use_smooth", [0] * self.mesh_data["total_polygons"])
        mesh.update()

        mesh["ios_materials"] = self.mesh_data["materials"]
        mesh["ios_material_ids"] = self.mesh_data["material_ids"]
        return mesh

    def apply_matrix_to_flat_coords(self, coords, matrix):
        coords_array = np.array(coords).reshape(-1, 3)
        ones = np.ones((coords_array.shape[0], 1))
        homogeneous_coords = np.hstack([coords_array, ones])
        transformed_coords = homogeneous_coords @ matrix.T
        return transformed_coords[:, :3].flatten().tolist()

    def convert_representation_item_face_based_surface_model(self, item):
        mesh = item.get_info_2(recursive=True)
        for face_set in mesh["FbsmFaces"]:
            self.convert_representation_item_face_set(item, face_set)

    def convert_representation_item_faceted_brep(self, item):
        mesh = item.get_info_2(recursive=True)
        return self.convert_representation_item_face_set(item, mesh["Outer"])

    def convert_representation_item_face_set(self, item, mesh):
        # On a few occasions, we flatten a list. This seems to be the most efficient way to do it.
        # https://stackoverflow.com/questions/20112776/how-do-i-flatten-a-list-of-lists-nested-lists

        # For huge face sets it might be better to do a "flatmap" instead of sum()
        # bounds = sum((f["Bounds"] for f in mesh["Outer"]["CfsFaces"] if len(f["Bounds"]) == 1), ())
        bounds = tuple(chain.from_iterable(f["Bounds"] for f in mesh["CfsFaces"] if len(f["Bounds"]) == 1))
        # Here are some untested alternatives, are they faster?
        # bounds = tuple((f["Bounds"] for f in mesh["Outer"]["CfsFaces"] if len(f["Bounds"]) == 1))[0]
        # bounds = chain.from_iterable(f["Bounds"] for f in mesh["Outer"]["CfsFaces"] if len(f["Bounds"]) == 1)

        polygons = [[(p["id"], p["Coordinates"]) for p in b["Bound"]["Polygon"]] for b in bounds]

        for face in mesh["CfsFaces"]:
            # Blender cannot handle faces with holes.
            if len(face["Bounds"]) > 1:
                inner_bounds = []
                inner_bound_point_ids = []
                for bound in face["Bounds"]:
                    if bound["type"] == "IfcFaceOuterBound":
                        outer_bound = [[p["Coordinates"] for p in bound["Bound"]["Polygon"]]]
                        outer_bound_point_ids = [[p["id"] for p in bound["Bound"]["Polygon"]]]
                    else:
                        inner_bounds.append([p["Coordinates"] for p in bound["Bound"]["Polygon"]])
                        inner_bound_point_ids.append([p["id"] for p in bound["Bound"]["Polygon"]])
                points = outer_bound[0].copy()
                [points.extend(p) for p in inner_bounds]
                point_ids = outer_bound_point_ids[0].copy()
                [point_ids.extend(p) for p in inner_bound_point_ids]

                tessellated_polygons = mathutils.geometry.tessellate_polygon(outer_bound + inner_bounds)
                polygons.extend([[(point_ids[pi], points[pi]) for pi in t] for t in tessellated_polygons])

        # Clever vertex welding algorithm by Thomas Krijnen. See #841.

        # by id
        di0 = {}
        # by coords
        di1 = {}

        vertex_index_offset = self.mesh_data["total_verts"]

        def lookup(id_coords):
            idx = di0.get(id_coords[0])
            if idx is None:
                idx = di1.get(id_coords[1])
                if idx is None:
                    l = len(di0)
                    di0[id_coords[0]] = l
                    di1[id_coords[1]] = l
                    return l + vertex_index_offset
                else:
                    return idx + vertex_index_offset
            else:
                return idx + vertex_index_offset

        mapped_polygons = [list(map(lookup, p)) for p in polygons]

        self.mesh_data["vertex_index"].extend(chain.from_iterable(mapped_polygons))

        # Flattened vertex coords
        self.mesh_data["co"].extend(chain.from_iterable(di1.keys()))
        self.mesh_data["total_verts"] += len(di1.keys())
        loop_total = [len(p) for p in mapped_polygons]
        total_polygons = len(mapped_polygons)
        self.mesh_data["total_polygons"] += total_polygons

        self.mesh_data["materials"].append(self.get_representation_item_material_name(item) or "NULLMAT")
        material_index = len(self.mesh_data["materials"]) - 1
        if self.mesh_data["materials"][material_index] == "NULLMAT":
            # Magic number -1 represents no material, until this has a better approach
            self.mesh_data["material_ids"] += [-1] * total_polygons
        else:
            self.mesh_data["material_ids"] += [material_index] * total_polygons

        if self.mesh_data["loop_start"]:
            loop_start_offset = self.mesh_data["loop_start"][-1] + self.mesh_data["loop_total"][-1]
        else:
            loop_start_offset = 0

        loop_start = [loop_start_offset] + [loop_start_offset + i for i in list(accumulate(loop_total[0:-1]))]
        self.mesh_data["loop_total"].extend(loop_total)
        self.mesh_data["loop_start"].extend(loop_start)
        # list(di1.keys())

    def create_native_swept_disk_solid(self, element, mesh_name, native_data):
        # TODO: georeferencing?
        curve = bpy.data.curves.new(mesh_name, type="CURVE")
        curve.dimensions = "3D"
        curve.resolution_u = 2
        polyline = curve.splines.new("POLY")

        for item_data in ifcopenshell.util.representation.resolve_items(native_data["representation"]):
            item = item_data["item"]
            matrix = item_data["matrix"]
            matrix[0][3] *= self.unit_scale
            matrix[1][3] *= self.unit_scale
            matrix[2][3] *= self.unit_scale
            # TODO: support inner radius, start param, and end param
            geometry = tool.Loader.create_generic_shape(item.Directrix)
            e = geometry.edges
            v = geometry.verts
            vertices = [list(matrix @ [v[i], v[i + 1], v[i + 2], 1]) for i in range(0, len(v), 3)]
            edges = [[e[i], e[i + 1]] for i in range(0, len(e), 2)]
            v2 = None
            for edge in edges:
                v1 = vertices[edge[0]]
                if v1 != v2:
                    polyline = curve.splines.new("POLY")
                    polyline.points[-1].co = native_data["matrix"] @ mathutils.Vector(v1)
                v2 = vertices[edge[1]]
                polyline.points.add(1)
                polyline.points[-1].co = native_data["matrix"] @ mathutils.Vector(v2)

        curve.bevel_depth = self.unit_scale * item.Radius
        curve.use_fill_caps = True
        return curve

    def merge_materials_by_colour(self):
        cleaned_materials = {}
        for m in bpy.data.materials:
            key = "-".join([str(x) for x in m.diffuse_color])
            cleaned_materials[key] = {"diffuse_color": m.diffuse_color}

        for cleaned_material in cleaned_materials.values():
            cleaned_material["material"] = bpy.data.materials.new("Merged Material")
            cleaned_material["material"].diffuse_color = cleaned_material["diffuse_color"]

        for obj in self.added_data.values():
            if not isinstance(obj, bpy.types.Object):
                continue
            if not hasattr(obj, "material_slots") or not obj.material_slots:
                continue
            for slot in obj.material_slots:
                m = slot.material
                key = "-".join([str(x) for x in m.diffuse_color])
                slot.material = cleaned_materials[key]["material"]

        for material in self.material_creator.materials.values():
            bpy.data.materials.remove(material)

    def add_project_to_scene(self):
        try:
            bpy.context.scene.collection.children.link(self.project["blender"])
        except:
            # Occurs when reloading a project
            pass
        project_collection = bpy.context.view_layer.layer_collection.children[self.project["blender"].name]
        if types_collection := project_collection.children.get("IfcTypeProduct"):
            types_collection.hide_viewport = False
            for obj in types_collection.collection.objects:  # turn off all objects inside Types collection.
                obj.hide_set(True)

    def clean_mesh(self):
        obj = None
        last_obj = None
        for obj in self.added_data.values():
            if not isinstance(obj, bpy.types.Object):
                continue
            if obj.type == "MESH":
                obj.select_set(True)
                last_obj = obj
        if not last_obj:
            return

        # Temporarily unhide types collection to make sure all objects will be cleaned
        project_collection = bpy.context.view_layer.layer_collection.children[self.project["blender"].name]
        if types_collection := project_collection.children.get("IfcTypeProduct"):
            types_collection.hide_viewport = False
            bpy.context.view_layer.objects.active = last_obj

        bpy.ops.object.editmode_toggle()
        bpy.ops.mesh.tris_convert_to_quads()
        bpy.ops.mesh.normals_make_consistent()
        bpy.ops.object.editmode_toggle()

        if types_collection:
            types_collection.hide_viewport = True
        bpy.context.view_layer.objects.active = last_obj
        IfcStore.edited_objs.clear()

    def load_file(self):
        self.ifc_import_settings.logger.info("loading file %s", self.ifc_import_settings.input_file)
        if not bpy.context.scene.BIMProperties.ifc_file:
            bpy.context.scene.BIMProperties.ifc_file = self.ifc_import_settings.input_file
        self.file = IfcStore.get_file()

    def calculate_unit_scale(self):
        self.unit_scale = ifcopenshell.util.unit.calculate_unit_scale(self.file)
        tool.Loader.set_unit_scale(self.unit_scale)

    def set_units(self):
        units = self.file.by_type("IfcUnitAssignment")[0]
        for unit in units.Units:
            if unit.is_a("IfcNamedUnit") and unit.UnitType == "LENGTHUNIT":
                if unit.is_a("IfcSIUnit"):
                    bpy.context.scene.unit_settings.system = "METRIC"
                    if unit.Name == "METRE":
                        if not unit.Prefix:
                            bpy.context.scene.unit_settings.length_unit = "METERS"
                        else:
                            bpy.context.scene.unit_settings.length_unit = f"{unit.Prefix}METERS"
                else:
                    bpy.context.scene.unit_settings.system = "IMPERIAL"
                    name = unit.Name.lower()
                    if name == "inch":
                        bpy.context.scene.unit_settings.length_unit = "INCHES"
                    elif name == "foot":
                        bpy.context.scene.unit_settings.length_unit = "FEET"
            elif unit.is_a("IfcNamedUnit") and unit.UnitType == "AREAUNIT":
                name = unit.Name if unit.is_a("IfcSIUnit") else unit.Name.lower()
                try:
                    bpy.context.scene.BIMProperties.area_unit = "{}{}".format(
                        unit.Prefix + "/" if hasattr(unit, "Prefix") and unit.Prefix else "", name
                    )
                except:  # Probably an invalid unit.
                    bpy.context.scene.BIMProperties.area_unit = "SQUARE_METRE"
            elif unit.is_a("IfcNamedUnit") and unit.UnitType == "VOLUMEUNIT":
                name = unit.Name if unit.is_a("IfcSIUnit") else unit.Name.lower()
                try:
                    bpy.context.scene.BIMProperties.volume_unit = "{}{}".format(
                        unit.Prefix + "/" if hasattr(unit, "Prefix") and unit.Prefix else "", name
                    )
                except:  # Probably an invalid unit.
                    bpy.context.scene.BIMProperties.volume_unit = "CUBIC_METRE"

    def create_project(self):
        project = self.file.by_type("IfcProject")[0]
        self.project = {"ifc": project}
        obj = tool.Ifc.get_object(project)
        if obj:
            self.project["blender"] = obj.BIMObjectProperties.collection
            self.has_existing_project = True
            return
        self.project["blender"] = bpy.data.collections.new(
            "{}/{}".format(self.project["ifc"].is_a(), self.project["ifc"].Name)
        )
        obj = self.create_product(self.project["ifc"])
        obj.hide_select = True
        self.project["blender"].objects.link(obj)
        self.project["blender"].BIMCollectionProperties.obj = obj
        obj.BIMObjectProperties.collection = self.collections[project.GlobalId] = self.project["blender"]

    def create_styles(self) -> None:
        for style in self.file.by_type("IfcSurfaceStyle"):
            self.create_style(style)

    def create_style(self, style: ifcopenshell.entity_instance) -> None:
        """Set up a Blender material for an IfcSurfaceStyle."""
        name = style.Name or str(style.id())
        blender_material = bpy.data.materials.new(name)
        blender_material.use_fake_user = True

        self.link_element(style, blender_material)
        self.material_creator.styles[style.id()] = blender_material

        style_elements = tool.Style.get_style_elements(blender_material)
        if tool.Style.has_blender_external_style(style_elements):
            blender_material.BIMStyleProperties.active_style_type = "External"
        else:
            blender_material.BIMStyleProperties.active_style_type = "Shading"

    def place_objects_in_collections(self) -> None:
        for ifc_definition_id, obj in self.added_data.items():
            if isinstance(obj, bpy.types.Object):
                tool.Collector.assign(obj, should_clean_users_collection=False)

    def is_curve_annotation(self, element: ifcopenshell.entity_instance) -> bool:
        object_type = element.ObjectType
        return (
            object_type in tool.Drawing.ANNOTATION_TYPES_DATA
            and tool.Drawing.ANNOTATION_TYPES_DATA[object_type][3] == "curve"
        )

    def get_drawing_group(self, element):
        for rel in element.HasAssignments or []:
            if rel.is_a("IfcRelAssignsToGroup") and rel.RelatingGroup.ObjectType == "DRAWING":
                return rel.RelatingGroup

    def get_element_matrix(self, element: ifcopenshell.entity_instance) -> np.ndarray:
        if isinstance(element, ifcopenshell.sqlite_entity):
            result = self.geometry_cache["shapes"][element.id()]["matrix"]
        else:
            result = ifcopenshell.util.placement.get_local_placement(element.ObjectPlacement)
        result[0][3] *= self.unit_scale
        result[1][3] *= self.unit_scale
        result[2][3] *= self.unit_scale
        return result

    def scale_matrix(self, matrix: np.array) -> np.array:
        matrix[0][3] *= self.unit_scale
        matrix[1][3] *= self.unit_scale
        matrix[2][3] *= self.unit_scale
        return matrix

    def get_representation_id(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a("IfcShapeRepresentation"):
                continue
            if (
                representation.RepresentationIdentifier == "Body"
                and representation.RepresentationType != "MappedRepresentation"
            ):
                return representation.id()
            elif representation.RepresentationIdentifier == "Body":
                return representation.Items[0].MappingSource.MappedRepresentation.id()

    def get_representation_cartesian_transformation(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a("IfcShapeRepresentation"):
                continue
            if (
                representation.RepresentationIdentifier == "Body"
                and representation.RepresentationType == "MappedRepresentation"
            ):
                return representation.Items[0].MappingTarget

    def create_curve(
        self,
        element: ifcopenshell.entity_instance,
        shape: Union[ifcopenshell.geom.ShapeElementType, ifcopenshell.geom.ShapeType],
    ) -> bpy.types.Curve:
        if hasattr(shape, "geometry"):
            geometry = shape.geometry
        else:
            geometry = shape

        curve = bpy.data.curves.new(tool.Loader.get_mesh_name_from_shape(geometry), type="CURVE")
        curve.dimensions = "3D"
        curve.resolution_u = 2

        e = geometry.edges
        v = geometry.verts
        vertices = [[v[i], v[i + 1], v[i + 2], 1] for i in range(0, len(v), 3)]
        edges = [[e[i], e[i + 1]] for i in range(0, len(e), 2)]
        v2 = None
        for edge in edges:
            v1 = vertices[edge[0]]
            if v1 != v2:
                polyline = curve.splines.new("POLY")
                polyline.points[-1].co = mathutils.Vector(v1)
            v2 = vertices[edge[1]]
            polyline.points.add(1)
            polyline.points[-1].co = mathutils.Vector(v2)
        return curve

    def create_mesh(
        self,
        element: ifcopenshell.entity_instance,
        shape: Union[ifcopenshell.geom.ShapeElementType, ifcopenshell.geom.ShapeType],
    ) -> bpy.types.Mesh:
        try:
            if hasattr(shape, "geometry"):
                # shape is ShapeElementType
                geometry = shape.geometry
            else:
                geometry = shape

            mesh = bpy.data.meshes.new(tool.Loader.get_mesh_name_from_shape(geometry))

            if geometry.verts and tool.Loader.is_point_far_away(
                (geometry.verts[0], geometry.verts[1], geometry.verts[2]), is_meters=True
            ):
                # Shift geometry close to the origin based off that first vert it found
                verts_array = np.array(geometry.verts)
                offset = np.array([-geometry.verts[0], -geometry.verts[1], -geometry.verts[2]])
                offset_verts = verts_array + np.tile(offset, len(verts_array) // 3)
                verts = offset_verts.tolist()

                mesh["has_cartesian_point_offset"] = True
                mesh["cartesian_point_offset"] = f"{geometry.verts[0]},{geometry.verts[1]},{geometry.verts[2]}"
            else:
                verts = geometry.verts
                mesh["has_cartesian_point_offset"] = False

            if geometry.faces:
                num_vertices = len(verts) // 3
                total_faces = len(geometry.faces)
                loop_start = range(0, total_faces, 3)
                num_loops = total_faces // 3
                loop_total = [3] * num_loops
                num_vertex_indices = len(geometry.faces)

                # See bug 3546
                # ios_edges holds true edges that aren't triangulated.
                #
                # we do `.tolist()` because Blender can't assign `np.int32` to it's custom attributes
                mesh["ios_edges"] = list(set(tuple(e) for e in ifcopenshell.util.shape.get_edges(geometry).tolist()))
                mesh["ios_item_ids"] = ifcopenshell.util.shape.get_representation_item_ids(geometry).tolist()

                mesh.vertices.add(num_vertices)
                mesh.vertices.foreach_set("co", verts)
                mesh.loops.add(num_vertex_indices)
                mesh.loops.foreach_set("vertex_index", geometry.faces)
                mesh.polygons.add(num_loops)
                mesh.polygons.foreach_set("loop_start", loop_start)
                mesh.polygons.foreach_set("loop_total", loop_total)
                mesh.polygons.foreach_set("use_smooth", [0] * total_faces)
                mesh.update()
            else:
                e = geometry.edges
                v = verts
                vertices = [[v[i], v[i + 1], v[i + 2]] for i in range(0, len(v), 3)]
                edges = [[e[i], e[i + 1]] for i in range(0, len(e), 2)]
                mesh.from_pydata(vertices, edges, [])

            mesh["ios_materials"] = [m.instance_id() for m in geometry.materials]
            mesh["ios_material_ids"] = geometry.material_ids
            return mesh
        except:
            self.ifc_import_settings.logger.error("Could not create mesh for %s", element)
            import traceback

            print(traceback.format_exc())

    def a2p(self, o: mathutils.Vector, z: mathutils.Vector, x: mathutils.Vector) -> mathutils.Matrix:
        y = z.cross(x)
        r = mathutils.Matrix((x, y, z, o))
        r.resize_4x4()
        r.transpose()
        return r

    def get_axis2placement(self, plc: ifcopenshell.entity_instance) -> mathutils.Matrix:
        if plc.is_a("IfcAxis2Placement3D"):
            z = mathutils.Vector(plc.Axis.DirectionRatios if plc.Axis else (0, 0, 1))
            x = mathutils.Vector(plc.RefDirection.DirectionRatios if plc.RefDirection else (1, 0, 0))
            o = plc.Location.Coordinates
        else:
            z = mathutils.Vector((0, 0, 1))
            if plc.RefDirection:
                x = mathutils.Vector(list(plc.RefDirection.DirectionRatios) + [0])
            else:
                x = mathutils.Vector((1, 0, 0))
            o = list(plc.Location.Coordinates) + [0]
        return self.a2p(o, z, x)

    def get_cartesiantransformationoperator(self, plc):
        x = mathutils.Vector(plc.Axis1.DirectionRatios if plc.Axis1 else (1, 0, 0))
        z = x.cross(mathutils.Vector(plc.Axis2.DirectionRatios if plc.Axis2 else (0, 1, 0)))
        o = plc.LocalOrigin.Coordinates
        return self.a2p(o, z, x)

    def get_local_placement(self, plc: Optional[ifcopenshell.entity_instance] = None) -> mathutils.Matrix:
        if plc is None:
            return mathutils.Matrix()
        if plc.PlacementRelTo is None:
            parent = mathutils.Matrix()
        else:
            parent = self.get_local_placement(plc.PlacementRelTo)
        return parent @ self.get_axis2placement(plc.RelativePlacement)

    def set_default_context(self):
        for subcontext in self.file.by_type("IfcGeometricRepresentationSubContext"):
            if subcontext.ContextIdentifier == "Body":
                bpy.context.scene.BIMRootProperties.contexts = str(subcontext.id())
                break

    def link_element(self, element: ifcopenshell.entity_instance, obj: IFC_CONNECTED_TYPE) -> None:
        self.added_data[element.id()] = obj
        tool.Ifc.link(element, obj)

    def set_matrix_world(self, obj: bpy.types.Object, matrix_world: mathutils.Matrix) -> None:
        obj.matrix_world = matrix_world
        tool.Geometry.record_object_position(obj)

    def setup_viewport_camera(self):
        context_override = tool.Blender.get_viewport_context()
        with bpy.context.temp_override(**context_override):
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.view3d.view_selected()
            bpy.ops.object.select_all(action="DESELECT")

    def setup_arrays(self):
        for element in self.file.by_type("IfcElement"):
            pset_data = ifcopenshell.util.element.get_pset(element, "BBIM_Array")
            if not pset_data or not pset_data.get("Data", None):  # skip array children
                continue
            for i in range(len(json.loads(pset_data["Data"]))):
                tool.Blender.Modifier.Array.set_children_lock_state(element, i, True)
                tool.Blender.Modifier.Array.constrain_children_to_parent(element)


class IfcImportSettings:
    def __init__(self):
        self.logger: logging.Logger = None
        self.input_file = None
        self.diff_file = None
        self.should_use_cpu_multiprocessing = True
        self.should_merge_materials_by_colour = False
        self.should_load_geometry = True
        self.should_use_native_meshes = False
        self.should_clean_mesh = False
        self.should_cache = True
        self.deflection_tolerance = 0.001
        self.angular_tolerance = 0.5
        self.void_limit = 30
        # Locations greater than 1km are not considered "small sites" according to the georeferencing guide
        # Users can configure this if they have to handle larger sites but beware of surveying precision
        self.distance_limit = 1000
        self.false_origin_mode = "AUTOMATIC"
        self.false_origin = None
        self.project_north = None
        self.element_offset = 0
        self.element_limit = 30000
        self.has_filter = None
        self.should_filter_spatial_elements = True
        self.should_setup_viewport_camera = True
        self.contexts = []
        self.context_settings: list[ifcopenshell.geom.main.settings] = []
        self.gross_context_settings: list[ifcopenshell.geom.main.settings] = []
        self.elements: set[ifcopenshell.entity_instance] = set()

    @staticmethod
    def factory(context=None, input_file=None, logger=None):
        scene_diff = bpy.context.scene.DiffProperties
        props = bpy.context.scene.BIMProjectProperties
        settings = IfcImportSettings()
        settings.input_file = input_file
        if logger is None:
            logger = logging.getLogger("ImportIFC")
        settings.logger = logger
        settings.diff_file = scene_diff.diff_json_file
        settings.should_use_cpu_multiprocessing = props.should_use_cpu_multiprocessing
        settings.should_merge_materials_by_colour = props.should_merge_materials_by_colour
        settings.should_load_geometry = props.should_load_geometry
        settings.should_use_native_meshes = props.should_use_native_meshes
        settings.should_clean_mesh = props.should_clean_mesh
        settings.should_cache = props.should_cache
        settings.deflection_tolerance = props.deflection_tolerance
        settings.angular_tolerance = props.angular_tolerance
        settings.void_limit = props.void_limit
        settings.distance_limit = props.distance_limit
        settings.false_origin_mode = props.false_origin_mode
        try:
            settings.false_origin = [float(o) for o in props.false_origin.split(",")[:3]]
        except:
            settings.false_origin = [0, 0, 0]
        try:
            settings.project_north = float(props.project_north)
        except:
            settings.project_north = 0
        settings.element_offset = props.element_offset
        settings.element_limit = props.element_limit
        return settings