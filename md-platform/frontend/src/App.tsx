import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAdmin, RequireAuth } from "./auth";
import { Layout } from "./components";
// Pages are owned by the "frontend pages" component (src/pages/). Each module
// is named after its route and provides a matching NAMED export:
//   src/pages/Login.tsx     export function Login()      -> /login
//   src/pages/Dashboard.tsx export function Dashboard()  -> /
//   src/pages/Upload.tsx    export function Upload()     -> /upload
//   src/pages/JobDetail.tsx export function JobDetail()  -> /jobs/:jobId
//   src/pages/Results.tsx   export function Results()    -> /jobs/:jobId/results
//   src/pages/Admin.tsx     export function Admin()      -> /admin
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { Upload } from "./pages/Upload";
import { JobDetail } from "./pages/JobDetail";
import { Results } from "./pages/Results";
import { Admin } from "./pages/Admin";

export default function App() {
  return (
    <Routes>
      {/* Public route: login lives outside the authenticated shell. */}
      <Route path="/login" element={<Login />} />

      {/* Authenticated routes share the persistent nav shell (Layout). */}
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/upload" element={<Upload />} />
        <Route path="/jobs/:jobId" element={<JobDetail />} />
        <Route path="/jobs/:jobId/results" element={<Results />} />
        <Route
          path="/admin"
          element={
            <RequireAdmin>
              <Admin />
            </RequireAdmin>
          }
        />
      </Route>

      {/* Unknown paths fall back to the dashboard (which re-guards to login). */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
