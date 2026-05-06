import { Routes, Route, BrowserRouter, Navigate } from "react-router-dom";
import { Toaster } from "./components/ui/sonner";
import { isAuthenticated } from "./lib/auth";
import AppLayout from "./components/AppLayout";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import AcceptInvite from "./pages/AcceptInvite";
import Team from "./pages/Team";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import Tasks from "./pages/Tasks";
import Lookahead from "./pages/Lookahead";
import DailyReports from "./pages/DailyReports";
import ProjectScopedList from "./pages/ProjectScopedList";
import GettingStarted from "./pages/GettingStarted";
import ResourceList, { Availability } from "./pages/Resources";
import Approvals from "./pages/Approvals";
import Recovery from "./pages/Recovery";
import DelayDetail from "./pages/DelayDetail";
import GatingEntityDetail from "./pages/GatingEntityDetail";
import CoordinationEvents from "./pages/CoordinationEvents";
import CoordinationEventDetail from "./pages/CoordinationEventDetail";
import Imports from "./pages/Imports";
import ScheduleProposals from "./pages/ScheduleProposals";
import Drawings from "./pages/Drawings";
import Specs from "./pages/Specs";
import Photos from "./pages/Photos";
import Spatial3D from "./pages/Spatial3D";
import PolygonEditor from "./pages/PolygonEditor";
import Submittals from "./pages/Submittals";
import Aging from "./pages/Aging";
import Productivity from "./pages/Productivity";
import MarketplaceFull from "./pages/MarketplaceFull";
import MasterDoc from "./pages/MasterDoc";
import KnowledgeBase from "./pages/KnowledgeBase";
import ComplianceFull from "./pages/ComplianceFull";
import VoicePinAdmin from "./pages/VoicePinAdmin";
import VoiceLogin from "./pages/VoiceLogin";
import MobileForeman from "./pages/MobileForeman";
import { AuditLog, AiAgentsPage, Settings, WeeklyReports } from "./pages/SystemPages";
import "./App.css";

function Guard({ children }) {
  if (!isAuthenticated()) return <Navigate to="/login" replace />;
  return children;
}

export default function App() {
  return (
    <div className="App">
      <Toaster />
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />
          <Route path="/accept-invite" element={<AcceptInvite />} />
          <Route path="/voice-login" element={<VoiceLogin />} />
          <Route path="/m/foreman" element={<Guard><MobileForeman /></Guard>} />
          <Route element={<Guard><AppLayout /></Guard>}>
            <Route index element={<Dashboard />} />
            <Route path="getting-started" element={<GettingStarted />} />
            <Route path="projects" element={<Projects />} />
            <Route path="team" element={<Team />} />

            {/* Daily ops */}
            <Route path="daily-reports" element={<DailyReports />} />
            <Route path="photos" element={<Photos />} />
            <Route path="delays" element={<ProjectScopedList kind="delays" />} />
            <Route path="delays/:id" element={<DelayDetail />} />
            <Route path="gating-entities/:id" element={<GatingEntityDetail />} />
            <Route path="coordination" element={<CoordinationEvents />} />
            <Route path="coordination/:id" element={<CoordinationEventDetail />} />
            <Route path="safety-events" element={<ProjectScopedList kind="safety-events" />} />
            <Route path="qc-issues" element={<ProjectScopedList kind="qc-issues" />} />

            {/* Planning */}
            <Route path="tasks" element={<Tasks />} />
            <Route path="lookahead" element={<Lookahead />} />
            <Route path="weekly-reports" element={<WeeklyReports />} />
            <Route path="locations" element={<ProjectScopedList kind="locations" />} />
            <Route path="spatial-3d" element={<Spatial3D />} />
            <Route path="polygon-editor" element={<PolygonEditor />} />

            {/* Procurement */}
            <Route path="rfis" element={<ProjectScopedList kind="rfis" />} />
            <Route path="deliveries" element={<ProjectScopedList kind="deliveries" />} />
            <Route path="drawings" element={<Drawings />} />
            <Route path="specs" element={<Specs />} />
            <Route path="submittals" element={<Submittals />} />
            <Route path="change-orders" element={<ProjectScopedList kind="change-orders" />} />
            <Route path="aging" element={<Aging />} />

            {/* Review */}
            <Route path="approvals" element={<Approvals />} />
            <Route path="work-items" element={<ProjectScopedList kind="work-items" />} />
            <Route path="recovery" element={<Recovery />} />

            {/* Resources */}
            <Route path="resources/crews" element={<ResourceList kind="crews" />} />
            <Route path="resources/equipment" element={<ResourceList kind="equipment" />} />
            <Route path="resources/people" element={<ResourceList kind="people" />} />
            <Route path="resources/certifications" element={<ResourceList kind="certifications" />} />
            <Route path="resources/availability" element={<Availability />} />
            <Route path="productivity" element={<Productivity />} />

            {/* System */}
            <Route path="imports" element={<Imports />} />
            <Route path="schedule-changes" element={<ScheduleProposals />} />
            <Route path="agents" element={<AiAgentsPage />} />
            <Route path="marketplace" element={<MarketplaceFull />} />
            <Route path="kb" element={<KnowledgeBase />} />
            <Route path="master-doc" element={<MasterDoc />} />
            <Route path="voice-pins" element={<VoicePinAdmin />} />
            <Route path="compliance" element={<ComplianceFull />} />
            <Route path="audit-log" element={<AuditLog />} />
            <Route path="settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </div>
  );
}
