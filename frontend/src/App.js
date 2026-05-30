import { Routes, Route, BrowserRouter, Navigate } from "react-router-dom";
import { Toaster } from "./components/ui/sonner";
import { isAuthenticated } from "./lib/auth";
import { ThemeProvider } from "./lib/theme";
import AppLayout from "./components/AppLayout";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import { ForgotPassword, ResetPassword } from "./pages/PasswordReset";
import AcceptInvite from "./pages/AcceptInvite";
import Team from "./pages/Team";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import Vendors from "./pages/Vendors";
import Tasks from "./pages/Tasks";
import Lookahead from "./pages/Lookahead";
import PaperworkProgress from "./pages/PaperworkProgress";
import DailyReports from "./pages/DailyReports";
import DrTemplate from "./pages/DrTemplate";
import ProjectScopedList from "./pages/ProjectScopedList";
import ResourceList, { Availability } from "./pages/Resources";
import Approvals from "./pages/Approvals";
import Recovery from "./pages/Recovery";
import DelayDetail from "./pages/DelayDetail";
import GatingEntityDetail from "./pages/GatingEntityDetail";
import CoordinationEvents from "./pages/CoordinationEvents";
import CoordinationEventDetail from "./pages/CoordinationEventDetail";
import Imports from "./pages/Imports";
import ScheduleProposals from "./pages/ScheduleProposals";
import ScheduleDiffs from "./pages/ScheduleDiffs";
import ScheduleDiffDetail from "./pages/ScheduleDiffDetail";
import CascadeDashboard from "./pages/CascadeDashboard";
import CalendarReconciliation from "./pages/CalendarReconciliation";
import TIAGenerator from "./pages/TIAGenerator";
import TIAGenerationsList from "./pages/TIAGenerationsList";
import TIAGenerationDetail from "./pages/TIAGenerationDetail";
import Drawings from "./pages/Drawings";
import Specs from "./pages/Specs";
import Photos from "./pages/Photos";
import PhotoTray from "./pages/PhotoTray";
import Spatial3D from "./pages/Spatial3D";
import PolygonEditor from "./pages/PolygonEditor";
import Submittals from "./pages/Submittals";
import Aging from "./pages/Aging";
import Productivity from "./pages/Productivity";
import MarketplaceFull from "./pages/MarketplaceFull";
import MasterDoc from "./pages/MasterDoc";
import KnowledgeBase from "./pages/KnowledgeBase";
import ComplianceFull from "./pages/ComplianceFull";
import MobileForeman from "./pages/MobileForeman";
import { AuditLog, AiAgentsPage, Settings, WeeklyReports } from "./pages/SystemPages";
import EditRequestsQueue from "./pages/EditRequestsQueue";
// Bucket H Phase 3 (2026-05-08) -- new pages.
import ResourceLeveling from "./pages/ResourceLeveling";
import ProjectAccess from "./pages/ProjectAccess";
import "./App.css";

function Guard({ children }) {
  // 2026-05-24 -- public landing now lives at /landing; unauthenticated
  // users land there first, with explicit "Sign in" CTAs that route to
  // /login. Keeps the login screen focused on the auth form.
  if (!isAuthenticated()) return <Navigate to="/landing" replace />;
  return children;
}

export default function App() {
  return (
    <ThemeProvider>
    <div className="App">
      <Toaster />
      <BrowserRouter>
        <Routes>
          {/* Public landing page (2026-05-24) -- holds the marketing
              pitch that used to sit beneath the Login card. Now the
              first page an unauthenticated user lands on. */}
          <Route path="/landing" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/accept-invite" element={<AcceptInvite />} />
          <Route path="/m/foreman" element={<Guard><MobileForeman /></Guard>} />
          <Route element={<Guard><AppLayout /></Guard>}>
            <Route index element={<Dashboard />} />
            <Route path="projects" element={<Projects />} />
            <Route path="vendors" element={<Vendors />} />
            <Route path="team" element={<Team />} />

            {/* Daily ops */}
            <Route path="daily-reports" element={<DailyReports />} />
            {/* Feature Client DR Template Export (2026-05-20) -- per-
                project page to upload the client's required xlsx
                daily-report template. Linked from the DR list page. */}
            <Route path="projects/:projectId/dr-template" element={<DrTemplate />} />
            <Route path="photos" element={<Photos />} />
            <Route path="photo-tray" element={<PhotoTray />} />
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
            <Route path="paperwork-progress" element={<PaperworkProgress />} />
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
            <Route path="cascade-dashboard" element={<CascadeDashboard />} />
            <Route path="calendar-reconciliation" element={<CalendarReconciliation />} />
            <Route path="tia" element={<TIAGenerator />} />
            <Route path="tia/history" element={<TIAGenerationsList />} />
            <Route path="tia/generations/:id" element={<TIAGenerationDetail />} />

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
            <Route path="schedule-updates" element={<ScheduleDiffs />} />
            <Route path="schedule-diffs/:id" element={<ScheduleDiffDetail />} />
            <Route path="agents" element={<AiAgentsPage />} />
            <Route path="marketplace" element={<MarketplaceFull />} />
            <Route path="kb" element={<KnowledgeBase />} />
            <Route path="master-doc" element={<MasterDoc />} />
            <Route path="compliance" element={<ComplianceFull />} />
            <Route path="audit-log" element={<AuditLog />} />
            <Route path="edit-requests" element={<EditRequestsQueue />} />
            {/* Bucket H Phase 3 -- Resource Leveling dashboard + Project Access. */}
            <Route path="resource-leveling" element={<ResourceLeveling />} />
            <Route path="project-access" element={<ProjectAccess />} />
            <Route path="settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </div>
    </ThemeProvider>
  );
}
