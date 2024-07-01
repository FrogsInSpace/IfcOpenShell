/********************************************************************************
 *                                                                              *
 * This file is part of IfcOpenShell.                                           *
 *                                                                              *
 * IfcOpenShell is free software: you can redistribute it and/or modify         *
 * it under the terms of the Lesser GNU General Public License as published by  *
 * the Free Software Foundation, either version 3.0 of the License, or          *
 * (at your option) any later version.                                          *
 *                                                                              *
 * IfcOpenShell is distributed in the hope that it will be useful,              *
 * but WITHOUT ANY WARRANTY; without even the implied warranty of               *
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the                 *
 * Lesser GNU General Public License for more details.                          *
 *                                                                              *
 * You should have received a copy of the Lesser GNU General Public License     *
 * along with this program. If not, see <http://www.gnu.org/licenses/>.         *
 *                                                                              *
 ********************************************************************************/

#include <map>
#include <set>

#include <stdmat.h>
#include <istdplug.h>

#include "IfcMax.h"

#include "../ifcgeom/Iterator.h"
#include "../ifcgeom/taxonomy.h"
#include "../ifcgeom/ConversionSettings.h"

static const int NUM_MATERIAL_SLOTS = 24;

BOOL WINAPI DllMain(HINSTANCE /*hinstDLL*/, ULONG /*fdwReason*/, LPVOID /*lpvReserved*/) {
    static int controlsInit = false;
	if (!controlsInit) {
		controlsInit = true;
		InitCommonControls();
	}
	return TRUE;
}

static class IFCImpClassDesc :public ClassDesc {
public:
    int                     IsPublic() { return 1; }
    void *                  Create(BOOL /*loading = FALSE*/) { return new IFCImp; }
    // TODO Delete() function?
    const TCHAR *			ClassName() { return _T("IFCImp"); }

#if MAX_VERSION_MAJOR >= 24
	const TCHAR *	NonLocalizedClassName() { return ClassName(); }
#endif 

    SClass_ID               SuperClassID() { return SCENE_IMPORT_CLASS_ID; }
    Class_ID                ClassID() { return Class_ID(0x3f230dbf, 0x5b3015c2); }
    const TCHAR*			Category() { return _T("Chrutilities"); }
} IFCImpDesc;

#define DLLEXPORT __declspec(dllexport)

extern "C" {

DLLEXPORT const TCHAR* LibDescription() {
    return _T("IfcOpenShell IFC Importer");
}

DLLEXPORT int LibNumberClasses() { return 1; }

DLLEXPORT ClassDesc* LibClassDesc(int i) {
    return i == 0 ? &IFCImpDesc : 0;
}

DLLEXPORT ULONG LibVersion() {
    return VERSION_3DSMAX;
}

} // extern "C"

int IFCImp::ExtCount() { return 1; }

const TCHAR * IFCImp::Ext(int n) {
	return n == 0 ? _T("IFC") : _T("");
}

const TCHAR * IFCImp::LongDesc() {
	return _T("IfcOpenShell IFC Importer for 3ds Max");
}

const TCHAR * IFCImp::ShortDesc() {
	return _T("Industry Foundation Classes");
}

const TCHAR * IFCImp::AuthorName() {
	return _T("Thomas Krijnen");
}

const TCHAR * IFCImp::CopyrightMessage() {
	return _T("Copyright (c) 2011-2016 IfcOpenShell");
}

const TCHAR * IFCImp::OtherMessage1() {
	return _T("");
}

const TCHAR * IFCImp::OtherMessage2() {
	return _T("");
}

unsigned int IFCImp::Version() {
	return 12;
}

// TODO Use this in IFCImp::ShowAbout() if/when wanted
//static BOOL CALLBACK AboutBoxDlgProc(HWND /*hWnd*/, UINT /*msg*/, WPARAM /*wParam*/, LPARAM /*lParam*/) {
//	return TRUE;
//}

void IFCImp::ShowAbout(HWND /*hWnd*/) {}

DWORD WINAPI fn(LPVOID /*arg*/) { return 0; }

#if MAX_RELEASE > 14000
# define S(x) (TSTR::FromCStr(x.c_str()))
#elif defined(_UNICODE)
# define S(x) (WStr(x.c_str()))
#else
# define S(x) (CStr(x.c_str()))
#endif

static Mtl* FindMaterialByName(MtlBaseLib* library, const std::string& material_name) {
    TSTR mat_name = S(material_name);
	const int mat_index = library->FindMtlByName(mat_name);
	Mtl* m = 0;
	if (mat_index != -1) {
		m = static_cast<Mtl*>((*library)[mat_index]);
	}
	return m;
}


static Mtl* FindOrCreateMaterial(MtlBaseLib* library, Interface* max_interface, int& slot, const ifcopenshell::geometry::taxonomy::style::ptr styleptr) {

	auto& style = *styleptr;
    std::string material_name = style.name;

	Mtl* m = FindMaterialByName(library, material_name);
	if (m == 0) {
		StdMat2* stdm = NewDefaultStdMat();
		const TimeValue t = -1;
		if (style.diffuse) {
            const ifcopenshell::geometry::taxonomy::colour diffuse = style.diffuse;
			stdm->SetDiffuse(Color(diffuse.r(), diffuse.g(), diffuse.b()),t);
		}
		if (style.specular) {
            const ifcopenshell::geometry::taxonomy::colour specular = style.specular;
			stdm->SetSpecular(Color(specular.r(), specular.g(), specular.b()),t);
		}
		if (style.has_specularity()) {
			stdm->SetShininess((float)style.specularity, t);
		}
		if (style.has_transparency()) {
			stdm->SetOpacity(1.0f - (float)style.transparency, t);
		}
		m = stdm;
		m->SetName(S(material_name));
		library->Add(m);
		if (slot < NUM_MATERIAL_SLOTS) {
			max_interface->PutMtlToMtlEditor(m,slot++);
		}
	}
	return m;
}


static Mtl* ComposeMultiMaterial(std::map<std::vector<std::string>, Mtl*>& multi_mats, MtlBaseLib* library,
    Interface* max_interface, int& slot, const std::vector<ifcopenshell::geometry::taxonomy::style::ptr> styleptrs,
    const std::string& object_type, const std::vector<int>& material_ids)
{
	std::vector<std::string> material_names;
	bool needs_default = std::find(material_ids.begin(), material_ids.end(), -1) != material_ids.end();

	if (needs_default) {
		material_names.push_back(object_type);
	}

	for (auto it = styleptrs.begin(); it != styleptrs.end(); ++it) {		
		material_names.push_back( (*it)->name);
	}

	Mtl* default_material = 0;
	if (needs_default) {
		default_material = FindMaterialByName(library, object_type);
		if (default_material == 0) {
			default_material = NewDefaultStdMat();
			default_material->SetName(S(object_type));
			library->Add(default_material);
			if (slot < NUM_MATERIAL_SLOTS) {
				max_interface->PutMtlToMtlEditor(default_material, slot++);
			}
		}
	}

	if (material_names.size() == 1) {
		if (needs_default) {
			return default_material;
		} else {
			return FindOrCreateMaterial(library, max_interface, slot, *styleptrs.begin());
		}
	}

	std::map<std::vector<std::string>, Mtl*>::const_iterator i = multi_mats.find(material_names);
	if (i != multi_mats.end()) {
		return i->second;
	}
	MultiMtl* multi_mat = NewDefaultMultiMtl();
	multi_mat->SetNumSubMtls((int)material_names.size());
	int mtl_id = 0;
	if (needs_default) {
		multi_mat->SetSubMtlAndName(mtl_id ++, default_material, default_material->GetName());
	}
	for (auto it = styleptrs.begin(); it != styleptrs.end(); ++it) {
		Mtl* mtl = FindOrCreateMaterial(library, max_interface, slot, *it);
		multi_mat->SetSubMtl(mtl_id ++, mtl);
	}
	library->Add(multi_mat);
	if (slot < NUM_MATERIAL_SLOTS) {
		max_interface->PutMtlToMtlEditor(multi_mat,slot++);
	}
	multi_mats.insert(std::pair<std::vector<std::string>, Mtl*>(material_names, multi_mat));
	return multi_mat;
}

int IFCImp::DoImport(const TCHAR *name, ImpInterface *impitfc, Interface *itfc, BOOL /*suppressPrompts*/) {

	ifcopenshell::geometry::Settings settings;
    settings.get<ifcopenshell::geometry::settings::UseWorldCoords>().value = false;
    settings.get<ifcopenshell::geometry::settings::WeldVertices>().value = true;
    
	// JW: is this a SEW_SHELLS/SewShells() equivalent ?
	// SewShells=true seems to have set GV_MAX_FACES_TO_ORIENT to infinity 
	settings.get<ifcopenshell::geometry::settings::ReorientShells>().value = true;

	// some settings which seem to make sense
	settings.get<ifcopenshell::geometry::settings::BuildingLocalPlacement>().value = true;
	settings.get<ifcopenshell::geometry::settings::DontEmitNormals>().value = true;
	//settings.get<ifcopenshell::geometry::settings::UseMaterialNames>().value = true;
	
	// enabling this crashes the iterator
	//settings.get<ifcopenshell::geometry::settings::UseElementHierarchy>().value = true;
	
	settings.get<ifcopenshell::geometry::settings::CircleSegments>().value = 32; // default is 16
	settings.get<ifcopenshell::geometry::settings::OutputDimensionality>().value = ifcopenshell::geometry::settings::CURVES_SURFACES_AND_SOLIDS; // default is SURFACES_AND_SOLIDS

#ifdef _UNICODE
	int fn_buffer_size = WideCharToMultiByte(CP_UTF8, 0, name, -1, 0, 0, 0, 0);
	char* fn_mb = new char[fn_buffer_size];
	WideCharToMultiByte(CP_UTF8, 0, name, -1, fn_mb, fn_buffer_size, 0, 0);
#else
	const char* fn_mb = name;
#endif

	IfcParse::IfcFile file(fn_mb);

	IfcGeom::Iterator iterator(settings, &file);

    delete[] fn_mb;
	if (!iterator.initialize())	return false;

	itfc->ProgressStart(_T("Importing file..."), TRUE, fn, NULL);

	MtlBaseLib* mats = itfc->GetSceneMtls();
	int slot = mats->Count();

	std::map<std::vector<std::string>, Mtl*> material_cache;

	do{
		const IfcGeom::Element* element = static_cast<const IfcGeom::Element*>(iterator.get());
		const IfcGeom::TriangulationElement* o = static_cast<const IfcGeom::TriangulationElement*>(iterator.get());

		const TSTR e_type = TSTR::FromUTF8(element->type().c_str());
		const TSTR e_guid = TSTR::FromUTF8(element->guid().c_str());
		const TSTR e_name = TSTR::FromUTF8(element->name().c_str());

		int e_id = element->id();

		const TSTR e_idStr = TSTR( std::to_wstring(e_id).c_str());

		Mtl *mat = ComposeMultiMaterial(material_cache, mats, itfc, slot, o->geometry().materials(), o->type(), o->geometry().material_ids());

		TriObject* tri = CreateNewTriObject();

		const int numVerts = (int)o->geometry().verts().size()/3;
		tri->mesh.setNumVerts(numVerts);
		for( int i = 0; i < numVerts; i ++ ) {
			tri->mesh.setVert(i,o->geometry().verts()[3*i+0],o->geometry().verts()[3*i+1],o->geometry().verts()[3*i+2]);
		}
		const int numFaces = (int)o->geometry().faces().size()/3;
		tri->mesh.setNumFaces(numFaces);

		bool needs_default = std::find(o->geometry().material_ids().begin(), o->geometry().material_ids().end(), -1) != o->geometry().material_ids().end();

		typedef std::pair<int, int> edge_t;

		std::set<edge_t> face_boundaries;
		for(std::vector<int>::const_iterator it = o->geometry().edges().begin(); it != o->geometry().edges().end();) {
			const int v1 = *it++;
			const int v2 = *it++;

			const edge_t e((std::min)(v1, v2), (std::max)(v1, v2));
			face_boundaries.insert(e);
		}

		for( int i = 0; i < numFaces; i ++ ) {
			const int v1 = o->geometry().faces()[3*i+0];
			const int v2 = o->geometry().faces()[3*i+1];
			const int v3 = o->geometry().faces()[3*i+2];
			
			const edge_t e1((std::min)(v1, v2), (std::max)(v1, v2));
			const edge_t e2((std::min)(v2, v3), (std::max)(v2, v3));
			const edge_t e3((std::min)(v3, v1), (std::max)(v3, v1));

			const bool b1 = face_boundaries.find(e1) != face_boundaries.end();
			const bool b2 = face_boundaries.find(e2) != face_boundaries.end();
			const bool b3 = face_boundaries.find(e3) != face_boundaries.end();

			tri->mesh.faces[i].setVerts(v1, v2, v3);
			tri->mesh.faces[i].setEdgeVisFlags(b1, b2, b3);

			MtlID mtlid = (MtlID)o->geometry().material_ids()[i];
			if (needs_default) {
				mtlid ++;
			}
			tri->mesh.faces[i].setMatID(mtlid);
		}
				
		tri->mesh.buildNormals();
		// Either use this or undefine the FACESETS_AS_COMPOUND option in IfcGeom.h to have
		// properly oriented normals. Using only the line below will result in a consistent
		// orientation of normals across shells, but not always oriented towards the
		// outside.
		// tri->mesh.UnifyNormals(false);
		tri->mesh.BuildStripsAndEdges();
		tri->mesh.InvalidateTopologyCache();
		tri->mesh.InvalidateGeomCache();

		ImpNode* node = impitfc->CreateNode();

		node->Reference(tri);
		
		const TSTR longName = e_type + _T("/") + e_name + _T("/#") + e_idStr;

		node->SetName(longName);

		node->GetINode()->Hide(o->type() == "IfcOpeningElement" || o->type() == "IfcSpace");
		if (mat) {
			node->GetINode()->SetMtl(mat);

			// set wirecolor to material color
			node->GetINode()->SetWireColor(mat->GetDiffuse().toRGB());
		}

		const auto &mtx = o->transformation().data()->ccomponents();

		node->SetTransform(0,Matrix3(  
			Point3(mtx(0,0), mtx(1,0), mtx(2,0)),
			Point3(mtx(0,1), mtx(1,1), mtx(2,1)),
			Point3(mtx(0,2), mtx(1,2), mtx(2,2)),
			Point3(mtx(0,3), mtx(1,3), mtx(2,3))
		));

		impitfc->AddNodeToScene(node);

		itfc->ProgressUpdate(iterator.progress(), true, _T(""));

	} while (iterator.next());

	itfc->ProgressEnd();
	
	return true;
}
