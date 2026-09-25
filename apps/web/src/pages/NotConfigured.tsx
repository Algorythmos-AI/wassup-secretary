import "../styles/signin.css";

/** Shown instead of the app when build settings are missing or wrong, rather than a blank page
 * or a sign-in that fails with a cryptic error. */
export function NotConfigured({ problems }: { problems: string[] }) {
  return (
    <main className="signin">
      <div className="signin__sheet">
        <h1 className="signin__product">WASSUP</h1>
        <p>This dashboard hasn't been set up yet, so sign-in isn't available.</p>
        <div className="signin__lede">
          <p>For the administrator: fix these settings on the web service, then deploy it again.</p>
          <ul>
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      </div>
    </main>
  );
}
