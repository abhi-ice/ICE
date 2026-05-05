// Add Spatial 3D to sidebar PLANNING & TRACKING section.
import {
  LayoutDashboard, FolderKanban, ClipboardList, Camera, AlertTriangle,
  ShieldAlert, ClipboardCheck, ListChecks, CalendarRange, FileBarChart,
  MapPin, MessagesSquare, Truck, FileText, BookOpen, FileSignature,
  GitPullRequest, Sparkles, Wrench, Users, HardHat, BadgeCheck,
  CalendarClock, FileUp, Bot, ShoppingBasket, ShieldCheck, History,
  Settings, Layers, Box, Activity, Clock, Building2, KeyRound, Brain,
} from "lucide-react";

export const SIDEBAR_SECTIONS = [
  {
    title: "OVERVIEW",
    items: [
      { label: "Dashboard", path: "/", icon: LayoutDashboard, testid: "sidebar-dashboard" },
      { label: "Getting started", path: "/getting-started", icon: BookOpen, testid: "sidebar-getting-started" },
      { label: "Projects", path: "/projects", icon: FolderKanban, testid: "sidebar-projects" },
      { label: "Master doc", path: "/master-doc", icon: Building2, testid: "sidebar-master-doc" },
    ],
  },
  {
    title: "DAILY OPERATIONS",
    items: [
      { label: "Daily reports", path: "/daily-reports", icon: ClipboardList, testid: "sidebar-daily-reports" },
      { label: "Photos", path: "/photos", icon: Camera, testid: "sidebar-photos" },
      { label: "Delays", path: "/delays", icon: AlertTriangle, testid: "sidebar-delays" },
      { label: "Safety", path: "/safety-events", icon: ShieldAlert, testid: "sidebar-safety" },
      { label: "QC issues", path: "/qc-issues", icon: ClipboardCheck, testid: "sidebar-qc" },
    ],
  },
  {
    title: "PLANNING & TRACKING",
    items: [
      { label: "Tasks", path: "/tasks", icon: ListChecks, testid: "sidebar-tasks" },
      { label: "Look ahead", path: "/lookahead", icon: CalendarRange, testid: "sidebar-lookahead" },
      { label: "Weekly reports", path: "/weekly-reports", icon: FileBarChart, testid: "sidebar-weekly-reports" },
      { label: "Locations", path: "/locations", icon: MapPin, testid: "sidebar-locations" },
      { label: "Polygon editor", path: "/polygon-editor", icon: MapPin, testid: "sidebar-polygon-editor" },
      { label: "Spatial 3D", path: "/spatial-3d", icon: Box, testid: "sidebar-spatial-3d" },
      { label: "Productivity", path: "/productivity", icon: Activity, testid: "sidebar-productivity" },
    ],
  },
  {
    title: "PROCUREMENT & DOCUMENTS",
    items: [
      { label: "RFIs", path: "/rfis", icon: MessagesSquare, badgeKey: "rfis", testid: "sidebar-rfis" },
      { label: "Deliveries", path: "/deliveries", icon: Truck, testid: "sidebar-deliveries" },
      { label: "Drawings", path: "/drawings", icon: FileText, testid: "sidebar-drawings" },
      { label: "Specifications", path: "/specs", icon: BookOpen, testid: "sidebar-specs" },
      { label: "Submittals", path: "/submittals", icon: FileSignature, badgeKey: "submittals", testid: "sidebar-submittals" },
      { label: "Change orders", path: "/change-orders", icon: GitPullRequest, testid: "sidebar-change-orders" },
      { label: "Aging", path: "/aging", icon: Clock, badgeKey: "aging", testid: "sidebar-aging" },
    ],
  },
  {
    title: "REVIEW & ACTIONS",
    items: [
      { label: "Approvals", path: "/approvals", icon: BadgeCheck, badgeKey: "approvals", testid: "sidebar-approvals" },
      { label: "Field issues", path: "/work-items", icon: Wrench, badgeKey: "work_items", testid: "sidebar-work-items" },
      { label: "Recovery", path: "/recovery", icon: Sparkles, testid: "sidebar-recovery" },
    ],
  },
  {
    title: "RESOURCES",
    items: [
      { label: "Crews", path: "/resources/crews", icon: Users, testid: "sidebar-crews" },
      { label: "Equipment", path: "/resources/equipment", icon: Wrench, testid: "sidebar-equipment" },
      { label: "People", path: "/resources/people", icon: HardHat, testid: "sidebar-people" },
      { label: "Certifications", path: "/resources/certifications", icon: BadgeCheck, testid: "sidebar-certifications" },
      { label: "Availability", path: "/resources/availability", icon: CalendarClock, testid: "sidebar-availability" },
    ],
  },
  {
    title: "SYSTEM",
    items: [
      { label: "Imports", path: "/imports", icon: FileUp, testid: "sidebar-imports" },
      { label: "AI agents", path: "/agents", icon: Bot, testid: "sidebar-agents" },
      { label: "Knowledge base", path: "/kb", icon: Brain, testid: "sidebar-kb" },
      { label: "Marketplace", path: "/marketplace", icon: ShoppingBasket, testid: "sidebar-marketplace" },
      { label: "Voice PINs", path: "/voice-pins", icon: KeyRound, testid: "sidebar-voice-pins" },
      { label: "Compliance", path: "/compliance", icon: ShieldCheck, testid: "sidebar-compliance" },
      { label: "Audit log", path: "/audit-log", icon: History, testid: "sidebar-audit" },
      { label: "Settings", path: "/settings", icon: Settings, testid: "sidebar-settings" },
    ],
  },
];

export const APP_BRAND = { name: "ICE Schedule Ops", icon: Layers };
