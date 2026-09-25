import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router";
import { useAuth } from "./auth/auth";
import { SessionProvider, useSession } from "./auth/session";
import { Shell } from "./components/Shell";
import { Analytics } from "./pages/Analytics";
import { Inbox } from "./pages/Inbox";
import { NoAccess } from "./pages/NoAccess";
import { SignIn } from "./pages/SignIn";
import { Team } from "./pages/Team";
import { Tv } from "./pages/Tv";
import { Usage } from "./pages/Usage";

function Loading() {
  return <p className="app-loading">Loading…</p>;
}

function ClinicGuard({ children }: { children: React.ReactNode }) {
  const { clinicId } = useParams();
  const { me, clinic } = useSession();
  if (!me) return <Loading />;
  if (!clinic(clinicId)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function SignedIn() {
  const { me, error } = useSession();
  if (error) return <NoAccess reason={error} />;
  if (!me) return <Loading />;
  const first = me.clinics[0];
  return (
    <Routes>
      <Route
        path="/c/:clinicId/tv"
        element={
          <ClinicGuard>
            <Tv />
          </ClinicGuard>
        }
      />
      <Route
        path="/c/:clinicId"
        element={
          <ClinicGuard>
            <Shell />
          </ClinicGuard>
        }
      >
        <Route index element={<Navigate to="inbox" replace />} />
        <Route path="inbox" element={<Inbox />} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="usage" element={<Usage />} />
        <Route path="team" element={<Team />} />
      </Route>
      <Route path="*" element={first ? <Navigate to={`/c/${first.id}/inbox`} replace /> : <NoAccess reason="no_access" />} />
    </Routes>
  );
}

export function App() {
  const { status } = useAuth();
  if (status === "loading") return <Loading />;
  if (status === "signed-out") return <SignIn />;
  return (
    <SessionProvider>
      <BrowserRouter>
        <SignedIn />
      </BrowserRouter>
    </SessionProvider>
  );
}
