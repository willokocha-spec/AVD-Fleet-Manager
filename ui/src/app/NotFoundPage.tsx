import { APP_NAME } from "@/lib/config";
import { ButtonLink, EmptyState, PageHeader } from "@/lib/components";
import { useEffect } from "react";

/** Catch-all route for unknown URLs. */
export function NotFoundPage() {
  useEffect(() => {
    document.title = `Not found · ${APP_NAME}`;
  }, []);

  return (
    <div className="page">
      <PageHeader
        title="Page not found"
        eyebrow="404"
        description="That route is not registered in this build of the console."
      />
      <EmptyState
        icon="search"
        title="Nothing at this address"
        description="The link may be stale, or the page may live behind a different route. Head back to the dashboard to continue."
        action={
          <ButtonLink to="/" variant="primary" icon="dashboard">
            Back to dashboard
          </ButtonLink>
        }
      />
    </div>
  );
}
