// 2.5D Mass Model + Spatial Path 3 elevation projection.
// Raw three.js (no R3F due to React 19 incompat).
// Bound elevation drawings — one per face (north/south/east/west) — are
// rendered to canvas via pdfjs and applied as THREE.CanvasTexture on a
// rectangular plane positioned at the corresponding building face.
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { pdfjs } from "react-pdf";
import api, { API } from "../lib/api";
import { getToken } from "../lib/auth";
import { useProjects } from "../lib/projects";
import { Box, Eye, EyeOff, MoveHorizontal, Layers as LayersIcon, ImageIcon, Scissors } from "lucide-react";
import SectionViewDialog from "../components/spatial/SectionViewDialog";

pdfjs.GlobalWorkerOptions.workerSrc = `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/${pdfjs.version}/pdf.worker.min.js`;

const STATUS_COLOR = {
  complete: 0x10b981,
  in_progress: 0x2563eb,
  blocked: 0xef4444,
  not_started: 0x94a3b8,
  deleted: 0x475569,
};
const CP_COLOR = 0xdc2626;

const FACE_AXES = {
  elevation_north: { name: "North", side: "z_max" },
  elevation_south: { name: "South", side: "z_min" },
  elevation_east:  { name: "East",  side: "x_max" },
  elevation_west:  { name: "West",  side: "x_min" },
};

export default function Spatial3D() {
  const { activeId, projects } = useProjects();
  const [floors, setFloors] = useState([]);
  const [locations, setLocations] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [drawings, setDrawings] = useState([]);
  const [meta, setMeta] = useState([]); // drawings_spatial_metadata
  const [showCP, setShowCP] = useState(true);
  const [showElev, setShowElev] = useState(true);
  const [sliceZ, setSliceZ] = useState(120);
  const [hover, setHover] = useState(null);
  const [bindOpen, setBindOpen] = useState(false);
  const [sectionOpen, setSectionOpen] = useState(false);
  const [sectionMode, setSectionMode] = useState(false);  // toggles 3D clip-plane
  const [sectionAngle, setSectionAngle] = useState(0);    // degrees, around Y axis
  const [sectionOffset, setSectionOffset] = useState(0);  // shift along normal
  const project = projects.find((p) => p.id === activeId);
  const mountRef = useRef(null);
  const sceneRef = useRef({});

  const refresh = async () => {
    const [f, l, t, d, m] = await Promise.all([
      api.get(`/projects/${activeId}/floors`),
      api.get(`/projects/${activeId}/locations`),
      api.get(`/projects/${activeId}/tasks`),
      api.get(`/projects/${activeId}/drawings`),
      api.get(`/projects/${activeId}/spatial-metadata`),
    ]);
    setFloors(f.data); setLocations(l.data); setTasks(t.data); setDrawings(d.data); setMeta(m.data);
  };

  useEffect(() => { activeId && refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [activeId]);

  const taskByLocation = useMemo(() => {
    const m = {};
    tasks.forEach((t) => {
      if (!t.location_id) return;
      const cur = m[t.location_id];
      if (!cur ||
          (t.status === "in_progress" && cur.status !== "in_progress") ||
          (t.is_critical_path && !cur.is_critical_path)) {
        m[t.location_id] = t;
      }
    });
    return m;
  }, [tasks]);

  // Building bounding box (XZ plan + Y stack height)
  const bounds = useMemo(() => {
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity, maxY = 0;
    locations.forEach((loc) => {
      const ring = loc.polygon_geojson?.coordinates?.[0];
      if (!ring) return;
      ring.forEach(([x, z]) => {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (z < minZ) minZ = z;
        if (z > maxZ) maxZ = z;
      });
    });
    floors.forEach((f) => {
      const top = (f.elevation_z_ft || 0) + (f.floor_height_ft || 12);
      if (top > maxY) maxY = top;
    });
    if (!isFinite(minX)) return null;
    return { minX, maxX, minZ, maxZ, maxY };
  }, [locations, floors]);

  // ---- Scene init (one-time) ----
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;
    const w = mount.clientWidth, h = mount.clientHeight;

    const scene = new THREE.Scene();
    // Red Noir: dark zinc-950 background instead of slate-100 light.
    // Matches the rest of the dark theme so the 3D canvas doesn't
    // appear as a bright white patch on the page.
    scene.background = new THREE.Color(0x0a0a0c);

    const camera = new THREE.PerspectiveCamera(45, w / h, 0.5, 3000);
    camera.position.set(220, 240, 280);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.shadowMap.enabled = true;
    renderer.localClippingEnabled = true;  // section view
    mount.innerHTML = "";
    mount.appendChild(renderer.domElement);

    // 3D section clip plane (master prompt §22 — true 3D section view)
    const clipPlane = new THREE.Plane(new THREE.Vector3(1, 0, 0), 0);
    sceneRef.currentClipPlane = clipPlane;
    // Visible cut plane indicator
    const planeMesh = new THREE.Mesh(
      new THREE.PlaneGeometry(800, 800),
      new THREE.MeshBasicMaterial({ color: 0xdc2626, side: THREE.DoubleSide,
                                    transparent: true, opacity: 0.18 }),
    );
    planeMesh.visible = false;
    scene.add(planeMesh);
    sceneRef.currentClipPlaneMesh = planeMesh;

    // Red Noir: darker hemisphere lighting (zinc tones) so the scene
    // doesn't get blown out by light-colored ambient bounce.
    scene.add(new THREE.HemisphereLight(0x71717a, 0x18181b, 0.6));
    const dir = new THREE.DirectionalLight(0xffffff, 0.9);
    dir.position.set(80, 120, 80); dir.castShadow = true; scene.add(dir);

    // Red Noir: dark ground plane (zinc-900) instead of slate-200 light.
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(600, 600),
      new THREE.MeshStandardMaterial({ color: 0x18181b })
    );
    ground.rotation.x = -Math.PI / 2; ground.position.y = -0.1; ground.receiveShadow = true;
    scene.add(ground);

    // Red Noir: subtle white-on-dark grid (zinc-700 / zinc-600) so the
    // grid is visible on the dark ground but doesn't blow out the scene.
    const grid = new THREE.GridHelper(600, 60, 0x52525b, 0x3f3f46);
    scene.add(grid);

    const buildingGroup = new THREE.Group();
    scene.add(buildingGroup);
    const elevationGroup = new THREE.Group();
    scene.add(elevationGroup);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true; controls.dampingFactor = 0.08;

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const onPointerMove = (e) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    };
    renderer.domElement.addEventListener("pointermove", onPointerMove);

    let raf = 0;
    const animate = () => {
      controls.update();
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(buildingGroup.children, true);
      const top = hits.find((h) => h.object.userData?.location);
      if (top) {
        const loc = top.object.userData.location;
        const task = top.object.userData.task;
        setHover({
          name: `${loc.code} ${loc.name}`,
          task: task ? `[${task.activity_code}] ${task.description.slice(0, 50)} (${task.pct_complete}%)` : "no activity",
        });
      } else if (hover) setHover(null);
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    };
    animate();

    const onResize = () => {
      const W = mount.clientWidth, H = mount.clientHeight;
      camera.aspect = W / H; camera.updateProjectionMatrix(); renderer.setSize(W, H);
    };
    window.addEventListener("resize", onResize);

    sceneRef.current = { scene, camera, renderer, buildingGroup, elevationGroup, controls };

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      controls.dispose(); renderer.dispose(); mount.innerHTML = "";
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Center camera once we have bounds ----
  useEffect(() => {
    const { camera, controls } = sceneRef.current;
    if (!camera || !controls || !bounds) return;
    const cx = (bounds.minX + bounds.maxX) / 2;
    const cz = (bounds.minZ + bounds.maxZ) / 2;
    const cy = bounds.maxY / 2;
    controls.target.set(cx, cy, cz);
    const span = Math.max(bounds.maxX - bounds.minX, bounds.maxZ - bounds.minZ, bounds.maxY) * 2.1;
    camera.position.set(cx + span * 0.7, cy + span * 0.7, cz + span * 0.9);
    controls.update();
  }, [bounds]);

  // ---- Re-build building when data or props change ----
  useEffect(() => {
    const { buildingGroup } = sceneRef.current;
    if (!buildingGroup) return;
    while (buildingGroup.children.length) {
      const o = buildingGroup.children[0];
      buildingGroup.remove(o);
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    }
    const floorsById = Object.fromEntries(floors.map((f) => [f.id, f]));
    locations.forEach((loc) => {
      const floor = floorsById[loc.floor_id];
      if (!floor) return;
      const poly = loc.polygon_geojson?.coordinates?.[0];
      if (!poly || poly.length < 3) return;
      const z0 = floor.elevation_z_ft || 0;
      if (z0 > sliceZ) return;
      const height = Math.min(floor.floor_height_ft || 12, sliceZ - z0);
      if (height <= 0) return;

      const task = taskByLocation[loc.id];
      const baseColor = task ? (STATUS_COLOR[task.status] || STATUS_COLOR.not_started) : STATUS_COLOR.not_started;
      const color = (showCP && task?.is_critical_path) ? CP_COLOR : baseColor;

      const shape = new THREE.Shape();
      poly.forEach(([x, y], i) => { i === 0 ? shape.moveTo(x, y) : shape.lineTo(x, y); });
      shape.closePath();
      const geom = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });

      const mat = new THREE.MeshStandardMaterial({
        color, transparent: true, opacity: 0.78, roughness: 0.7,
        clippingPlanes: sceneRef.currentClipPlane && sectionMode ? [sceneRef.currentClipPlane] : [],
        clipShadows: true,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.rotation.x = -Math.PI / 2; mesh.position.y = z0;
      mesh.castShadow = true; mesh.receiveShadow = true;
      mesh.userData = { location: loc, task };
      buildingGroup.add(mesh);

      const edgesGeom = new THREE.EdgesGeometry(geom);
      const lineMat = new THREE.LineBasicMaterial({
        color: 0x0f172a, transparent: true, opacity: 0.4,
        clippingPlanes: sceneRef.currentClipPlane && sectionMode ? [sceneRef.currentClipPlane] : [],
      });
      const lines = new THREE.LineSegments(edgesGeom, lineMat);
      lines.rotation.x = -Math.PI / 2; lines.position.y = z0;
      buildingGroup.add(lines);
    });
  }, [floors, locations, taskByLocation, showCP, sliceZ, sectionMode]);

  // ---- Update clip plane orientation when angle/offset changes ----
  useEffect(() => {
    const cp = sceneRef.currentClipPlane;
    const cpMesh = sceneRef.currentClipPlaneMesh;
    if (!cp || !cpMesh) return;
    const rad = (sectionAngle * Math.PI) / 180;
    const normal = new THREE.Vector3(Math.cos(rad), 0, Math.sin(rad));
    cp.normal.copy(normal);
    cp.constant = -sectionOffset;
    cpMesh.visible = sectionMode;
    if (sectionMode && bounds) {
      const cx = (bounds.minX + bounds.maxX) / 2;
      const cy = (bounds.maxY) / 2;
      const cz = (bounds.minZ + bounds.maxZ) / 2;
      cpMesh.position.set(cx + normal.x * sectionOffset, cy, cz + normal.z * sectionOffset);
      cpMesh.lookAt(cpMesh.position.clone().add(normal));
    }
  }, [sectionAngle, sectionOffset, sectionMode, bounds]);

  // ---- Elevation projection (Spatial Path 3) ----
  useEffect(() => {
    const { elevationGroup } = sceneRef.current;
    if (!elevationGroup || !bounds) return;
    while (elevationGroup.children.length) {
      const o = elevationGroup.children[0];
      elevationGroup.remove(o);
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        if (o.material.map) o.material.map.dispose();
        o.material.dispose();
      }
    }
    if (!showElev) return;

    const elevations = meta.filter((m) => m.drawing_type?.startsWith("elevation_"));
    elevations.forEach(async (em) => {
      try {
        // Fetch the PDF via authed endpoint, render page 1 to canvas
        const res = await fetch(`${API}/drawing-revisions/${em.drawing_revision_id}/pdf`, {
          headers: { Authorization: `Bearer ${getToken()}` },
          credentials: "include",
        });
        if (!res.ok) return;
        const blob = await res.blob();
        const arrayBuffer = await blob.arrayBuffer();
        const pdf = await pdfjs.getDocument({ data: arrayBuffer }).promise;
        const pageNum = Math.max(1, em.page_number || 1);
        const page = await pdf.getPage(pageNum);
        const viewport = page.getViewport({ scale: 1.5 });
        const canvas = document.createElement("canvas");
        canvas.width = viewport.width; canvas.height = viewport.height;
        const ctx = canvas.getContext("2d");
        await page.render({ canvasContext: ctx, viewport }).promise;
        const tex = new THREE.CanvasTexture(canvas);
        tex.colorSpace = THREE.SRGBColorSpace;

        // Plane sized to building face
        const W = bounds.maxX - bounds.minX;
        const D = bounds.maxZ - bounds.minZ;
        const H = bounds.maxY;

        let geom, mesh;
        const dt = em.drawing_type;
        if (dt === "elevation_north" || dt === "elevation_south") {
          geom = new THREE.PlaneGeometry(W, H);
          mesh = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.85, side: THREE.DoubleSide }));
          mesh.position.set((bounds.minX + bounds.maxX) / 2, H / 2, dt === "elevation_north" ? bounds.maxZ + 0.3 : bounds.minZ - 0.3);
          if (dt === "elevation_south") mesh.rotation.y = Math.PI;
        } else if (dt === "elevation_east" || dt === "elevation_west") {
          geom = new THREE.PlaneGeometry(D, H);
          mesh = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.85, side: THREE.DoubleSide }));
          mesh.position.set(dt === "elevation_east" ? bounds.maxX + 0.3 : bounds.minX - 0.3, H / 2, (bounds.minZ + bounds.maxZ) / 2);
          mesh.rotation.y = dt === "elevation_east" ? -Math.PI / 2 : Math.PI / 2;
        } else { return; }

        mesh.userData = { elevation: em };
        elevationGroup.add(mesh);
      } catch (e) {
        // best-effort; skip on error
      }
    });
  }, [meta, bounds, showElev]);

  return (
    <div data-testid="spatial-3d-page" className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Box className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">2.5D Mass Model + Elevations</h1>
          </div>
          <p className="text-sm text-slate-500">
            {project?.name} · {floors.length} floors · {locations.length} rooms · {meta.filter((m) => m.drawing_type?.startsWith("elevation_")).length} elevation faces bound
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button data-testid="bind-elev" onClick={() => setBindOpen(true)} className="h-8 px-3 rounded text-xs flex items-center gap-1 bg-white border border-slate-200">
            <ImageIcon className="h-3.5 w-3.5" /> Bind elevation drawings
          </button>
          <button data-testid="open-section" onClick={() => setSectionOpen(true)} className="h-8 px-3 rounded text-xs flex items-center gap-1 bg-white border border-slate-200">
            <Scissors className="h-3.5 w-3.5" /> 2D section
          </button>
          <button
            data-testid="toggle-section-3d"
            onClick={() => setSectionMode((s) => !s)}
            className={`h-8 px-3 rounded text-xs flex items-center gap-1 border ${sectionMode ? "bg-rose-600 text-white border-rose-600" : "bg-white border-slate-200"}`}
          >
            <Scissors className="h-3.5 w-3.5" /> 3D cut
          </button>
          <button
            data-testid="toggle-elev"
            onClick={() => setShowElev(!showElev)}
            className={`h-8 px-3 rounded text-xs flex items-center gap-1 ${showElev ? "bg-blue-600 text-white" : "bg-white border border-slate-200"}`}
          >
            <LayersIcon className="h-3.5 w-3.5" />
            Elevations
          </button>
          <button
            data-testid="toggle-cp"
            onClick={() => setShowCP(!showCP)}
            className={`h-8 px-3 rounded text-xs flex items-center gap-1 ${showCP ? "bg-red-600 text-white" : "bg-white border border-slate-200"}`}
          >
            {showCP ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
            CP highlight
          </button>
          <div className="flex items-center gap-2 px-3 py-1 bg-white border border-slate-200 rounded text-xs">
            <MoveHorizontal className="h-3.5 w-3.5" />
            <span className="text-slate-500">Slice Z:</span>
            <input data-testid="slice-z" type="range" min="0" max="120" step="2" value={sliceZ} onChange={(e) => setSliceZ(Number(e.target.value))} className="w-32" />
            <span className="font-mono w-10 text-right">{sliceZ}'</span>
          </div>
          {sectionMode && (
            <div data-testid="section-3d-controls" className="flex items-center gap-3 px-3 py-1 bg-rose-50 border border-rose-200 rounded text-xs">
              <span className="text-rose-700 font-medium">3D cut</span>
              <span className="text-slate-500">Angle:</span>
              <input data-testid="section-angle" type="range" min="0" max="180" step="5" value={sectionAngle} onChange={(e) => setSectionAngle(Number(e.target.value))} className="w-24" />
              <span className="font-mono w-10 text-right">{sectionAngle}°</span>
              <span className="text-slate-500">Offset:</span>
              <input data-testid="section-offset" type="range" min={-200} max={200} step="2" value={sectionOffset} onChange={(e) => setSectionOffset(Number(e.target.value))} className="w-24" />
              <span className="font-mono w-12 text-right">{sectionOffset}'</span>
            </div>
          )}
        </div>
      </div>

      <div className="surface relative overflow-hidden" style={{ height: "calc(100vh - 220px)" }}>
        <div ref={mountRef} data-testid="spatial-3d-canvas" className="w-full h-full" />
        {hover && (
          <div className="absolute top-3 left-3 bg-slate-900 text-white px-3 py-2 rounded text-xs pointer-events-none">
            <div className="font-medium">{hover.name}</div>
            <div className="opacity-80">{hover.task}</div>
          </div>
        )}
      </div>

      <div className="surface p-3 flex flex-wrap gap-3 text-xs">
        <Legend color="#94a3b8" label="Not started" />
        <Legend color="#2563eb" label="In progress" />
        <Legend color="#10b981" label="Complete" />
        <Legend color="#ef4444" label="Blocked" />
        <Legend color="#dc2626" label="Critical path" />
      </div>

      {bindOpen && (
        <BindElevationDialog
          drawings={drawings}
          meta={meta}
          onClose={async () => { setBindOpen(false); await refresh(); }}
        />
      )}

      {sectionOpen && (() => {
        // Pick a plan revision: prefer one bound as drawing_type='plan', else first
        const planMeta = meta.find((m) => m.drawing_type === "plan");
        const planRev = planMeta?.drawing_revision_id
          || drawings.find((d) => d.current_revision_id)?.current_revision_id;
        if (!planRev) {
          return (
            <div className="fixed inset-0 z-50 bg-slate-900/60 grid place-items-center">
              <div className="bg-white rounded p-5 w-full max-w-[400px] mx-3">
                <div className="font-medium mb-2">No plan revision available</div>
                <p className="text-xs text-slate-500 mb-3">Upload a plan PDF in <a href="/drawings" className="text-blue-600 hover:underline">Drawings</a> first.</p>
                <button onClick={() => setSectionOpen(false)} className="h-9 px-3 rounded bg-slate-200 text-sm">Close</button>
              </div>
            </div>
          );
        }
        return (
          <SectionViewDialog
            projectId={activeId}
            planRevisionId={planRev}
            onClose={() => setSectionOpen(false)}
          />
        );
      })()}
    </div>
  );
}

const Legend = ({ color, label }) => (
  <div className="flex items-center gap-1.5">
    <span className="h-3 w-5 rounded" style={{ background: color }} />
    <span>{label}</span>
  </div>
);

function BindElevationDialog({ drawings, meta, onClose }) {
  const [revisions, setRevisions] = useState([]);
  useEffect(() => {
    const ids = drawings.filter((d) => d.current_revision_id).map((d) => d.current_revision_id);
    if (!ids.length) { setRevisions([]); return; }
    Promise.all(ids.map((id) =>
      api.get(`/drawing-revisions/${id}`).then((r) => ({ ...r.data, drawing: drawings.find((d) => d.current_revision_id === id) })).catch(() => null)
    )).then((all) => setRevisions(all.filter(Boolean)));
  }, [drawings]);

  const metaByRev = Object.fromEntries(meta.map((m) => [`${m.drawing_revision_id}|${m.page_number || 1}`, m]));

  const setFace = async (rev, type) => {
    await api.post("/drawings-spatial-metadata", {
      drawing_revision_id: rev.id,
      page_number: 1,
      drawing_type: type,
      calibrated: false,
    });
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/60 grid place-items-center" data-testid="bind-elev-dialog">
      <div className="bg-white rounded-md p-5 w-[640px] max-h-[80vh] overflow-y-auto">
        <div className="font-medium mb-3">Bind elevation drawings to building faces</div>
        <p className="text-xs text-slate-500 mb-3">Pick a face for each elevation revision. The PDF will be rendered as a texture and projected onto the corresponding exterior face of the 2.5D model.</p>
        <table className="dense-table w-full">
          <thead><tr><th>Sheet</th><th>Title</th><th>Current binding</th><th>Set face</th></tr></thead>
          <tbody>
            {revisions.map((r) => {
              const m = metaByRev[`${r.id}|1`];
              return (
                <tr key={r.id}>
                  <td className="font-mono text-xs">{r.drawing?.sheet_number}</td>
                  <td className="text-xs truncate max-w-[200px]">{r.drawing?.title}</td>
                  <td className="text-xs">
                    {m ? <span className="pill pill-blue">{m.drawing_type}</span> : <span className="text-slate-400">unbound</span>}
                  </td>
                  <td>
                    <select
                      data-testid={`bind-face-${r.id}`}
                      defaultValue={m?.drawing_type || ""}
                      onChange={(e) => setFace(r, e.target.value)}
                      className="h-7 px-2 rounded border border-slate-200 text-xs bg-white"
                    >
                      <option value="">— pick —</option>
                      <option value="plan">Plan</option>
                      <option value="elevation_north">Elevation North</option>
                      <option value="elevation_south">Elevation South</option>
                      <option value="elevation_east">Elevation East</option>
                      <option value="elevation_west">Elevation West</option>
                    </select>
                  </td>
                </tr>
              );
            })}
            {!revisions.length && <tr><td colSpan="4" className="text-center text-slate-500 py-4 text-sm">No drawing revisions uploaded yet. Visit Drawings → Upload PDF.</td></tr>}
          </tbody>
        </table>
        <div className="flex justify-end mt-4">
          <button data-testid="bind-elev-close" onClick={onClose} className="h-9 px-3 rounded bg-blue-600 text-white text-sm">Done</button>
        </div>
      </div>
    </div>
  );
}
