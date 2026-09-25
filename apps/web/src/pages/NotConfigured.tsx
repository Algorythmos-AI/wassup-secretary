import "../styles/signin.css";

/** Shown instead of the app when the build is missing settings, rather than a blank page. */
export function NotConfigured({ missing }: { missing: string[] }) {
  return (
    <main className="signin">
      <div className="signin__sheet">
        <h1 className="signin__product">WASSUP</h1>
        <p>This dashboard hasn't been set up yet, so sign-in isn't available.</p>
        <p className="signin__lede">
          For the administrator: set {missing.join(", ")} on the web service, then deploy it again.
        </p>
      </div>
    </main>
  );
}
