// 2.5D Mass Model — raw three.js (no react-three-fiber).
// Bypasses the R3F + React 19 __source prop incompatibility (the
// "Cannot set x-line-number" error).
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import api from "../lib/api";
import { useProjects } from "../lib/projects";
import { Box, Eye, EyeOff, MoveHorizontal } from "lucide-react";

const STATUS_COLOR = {
  complete: 0x10b981,
  in_progress: 0x2563eb,
  blocked: 0xef4444,
  not_started: 0x94a3b8,
  deleted: 0x475569,
};
const CP_COLOR = 0xdc2626;

export default function Spatial3D() {
  const { activeId, projects } = useProjects();
  const [floors, setFloors] = useState([]);
  const [locations, setLocations] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [showCP, setShowCP] = useState(true);
  const [sliceZ, setSliceZ] = useState(120);
  const [hover, setHover] = useState(null);
  const project = projects.find((p) => p.id === activeId);
  const mountRef = useRef(null);
  const sceneRef = useRef({});

  useEffect(() => {
    if (!activeId) return;
    api.get(`/projects/${activeId}/floors`).then(({ data }) => setFloors(data));
    api.get(`/projects/${activeId}/locations`).then(({ data }) => setLocations(data));
    api.get(`/projects/${activeId}/tasks`).then(({ data }) => setTasks(data));
  }, [activeId]);

  // Map location_id → assigned task
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

  // ---- Scene setup (one-time) ----
  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    const w = mount.clientWidth;
    const h = mount.clientHeight;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf1f5f9);

    const camera = new THREE.PerspectiveCamera(45, w / h, 0.5, 2000);
    camera.position.set(180, 200, 220);
    camera.lookAt(50, 50, 25);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.shadowMap.enabled = true;
    mount.innerHTML = "";
    mount.appendChild(renderer.domElement);

    const hemi = new THREE.HemisphereLight(0xe2e8f0, 0x94a3b8, 0.7);
    scene.add(hemi);
    const dir = new THREE.DirectionalLight(0xffffff, 0.9);
    dir.position.set(80, 120, 80);
    dir.castShadow = true;
    scene.add(dir);

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(400, 400),
      new THREE.MeshStandardMaterial({ color: 0xe2e8f0 })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.set(50, -0.1, 25);
    ground.receiveShadow = true;
    scene.add(ground);

    const grid = new THREE.GridHelper(400, 40, 0xcbd5e1, 0xe2e8f0);
    grid.position.set(50, 0, 25);
    scene.add(grid);

    const buildingGroup = new THREE.Group();
    scene.add(buildingGroup);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(50, 30, 25);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    // raycaster for hover
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
      // hover detection
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
      } else if (hover) {
        setHover(null);
      }
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    };
    animate();

    const onResize = () => {
      const W = mount.clientWidth, H = mount.clientHeight;
      camera.aspect = W / H;
      camera.updateProjectionMatrix();
      renderer.setSize(W, H);
    };
    window.addEventListener("resize", onResize);

    sceneRef.current = { scene, camera, renderer, buildingGroup, controls };

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      controls.dispose();
      renderer.dispose();
      mount.innerHTML = "";
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Re-build building when data or props change ----
  useEffect(() => {
    const { buildingGroup } = sceneRef.current;
    if (!buildingGroup) return;
    // dispose old
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

      const mat = new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.78, roughness: 0.7 });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.y = z0;
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.userData = { location: loc, task };
      buildingGroup.add(mesh);

      // wireframe edges
      const edgesGeom = new THREE.EdgesGeometry(geom);
      const lineMat = new THREE.LineBasicMaterial({ color: 0x0f172a, transparent: true, opacity: 0.4 });
      const lines = new THREE.LineSegments(edgesGeom, lineMat);
      lines.rotation.x = -Math.PI / 2;
      lines.position.y = z0;
      buildingGroup.add(lines);
    });
  }, [floors, locations, taskByLocation, showCP, sliceZ]);

  return (
    <div data-testid="spatial-3d-page" className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Box className="h-5 w-5 text-blue-600" />
            <h1 className="text-2xl font-semibold tracking-tight">2.5D Mass Model</h1>
          </div>
          <p className="text-sm text-slate-500">
            {project?.name} · {floors.length} floors · {locations.length} rooms · color = activity status
          </p>
        </div>
        <div className="flex items-center gap-3">
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
    </div>
  );
}

const Legend = ({ color, label }) => (
  <div className="flex items-center gap-1.5">
    <span className="h-3 w-5 rounded" style={{ background: color }} />
    <span>{label}</span>
  </div>
);
