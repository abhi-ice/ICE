import { Routes, Route, BrowserRouter, Navigate } from "react-router-dom";
import { Toaster } from "./components/ui/sonner";
import { isAuthenticated } from "./lib/auth";
import AppLayout from "./components/AppLayout";
import Login from "./pages/Login";
import AuthCallback from "./pages/AuthCallback";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import Tasks from "./pages/Tasks";
import Lookahead from "./pages/Lookahead";
import DailyReports from "./pages/DailyReports";
import ProjectScopedList from "./pages/ProjectScopedList";
import ResourceList, { Availability } from "./pages/Resources";
import Approvals from "./pages/Approvals";
import Recovery from "./pages/Recovery";
import Imports from "./pages/Imports";
import Drawings from "./pages/Drawings";
import Specs from "./pages/Specs";
import { AuditLog, AiAgentsPage, Compliance, Marketplace, Settings, WeeklyReports } from "./pages/SystemPages";
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
          <Route path="/auth/callback" element={<AuthCallback />} />
          <Route element={<Guard><AppLayout /></Guard>}>
            <Route index element={<Dashboard />} />
            <Route path="projects" element={<Projects />} />

            {/* Daily ops */}
            <Route path="daily-reports" element={<DailyReports />} />
            <Route path="photos" element={<ProjectScopedList kind="photos" />} />
            <Route path="delays" element={<ProjectScopedList kind="delays" />} />
            <Route path="safety-events" element={<ProjectScopedList kind="safety-events" />} />
            <Route path="qc-issues" element={<ProjectScopedList kind="qc-issues" />} />

            {/* Planning */}
            <Route path="tasks" element={<Tasks />} />
            <Route path="lookahead" element={<Lookahead />} />
            <Route path="weekly-reports" element={<WeeklyReports />} />
            <Route path="locations" element={<ProjectScopedList kind="locations" />} />

            {/* Procurement */}
            <Route path="rfis" element={<ProjectScopedList kind="rfis" />} />
            <Route path="deliveries" element={<ProjectScopedList kind="deliveries" />} />
            <Route path="drawings" element={<Drawings />} />
            <Route path="specs" element={<Specs />} />
            <Route path="submittals" element={<ProjectScopedList kind="submittals" />} />
            <Route path="change-orders" element={<ProjectScopedList kind="change-orders" />} />

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

            {/* System */}
            <Route path="imports" element={<Imports />} />
            <Route path="agents" element={<AiAgentsPage />} />
            <Route path="marketplace" element={<Marketplace />} />
            <Route path="compliance" element={<Compliance />} />
            <Route path="audit-log" element={<AuditLog />} />
            <Route path="settings" element={<Settings />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </div>
  );
}
