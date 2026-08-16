export const PAGE_IDS = [
  "work",
  "submit",
  "requirements",
  "tasks",
  "review",
  "chat",
  "genome",
  "roster",
  "settings",
  "insights",
  "new-project",
] as const;

export type PageId = (typeof PAGE_IDS)[number];

export type Route =
  | { readonly page: PageId; readonly tab?: string }
  | { readonly page: "tasks"; readonly taskId: string }
  | { readonly page: "requirements"; readonly requirementId: string }
  | { readonly page: "project-created"; readonly workspace: string }
  | { readonly page: "not-found"; readonly requested: string };

const PAGE_SET: ReadonlySet<string> = new Set(PAGE_IDS);
const GENOME_TABS: ReadonlySet<string> = new Set(["map", "knowledge", "lessons", "rules", "procedures"]);
const INSIGHT_TABS: ReadonlySet<string> = new Set(["activity", "trends", "costs", "audit", "notify"]);

function decoded(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

export function parseHash(hash: string): Route {
  const requested = hash.replace(/^#/, "") || "/work";
  const parts = requested.split("/").filter(Boolean);
  const page = parts[0] ?? "work";

  if ((page === "tasks" || page === "requirements") && parts.length === 2) {
    const id = decoded(parts[1]!);
    if (!id) return { page: "not-found", requested };
    return page === "tasks"
      ? { page: "tasks", taskId: id }
      : { page: "requirements", requirementId: id };
  }
  if (page === "project-created" && parts.length === 2) {
    const workspace = decoded(parts[1]!);
    return workspace
      ? { page: "project-created", workspace }
      : { page: "not-found", requested };
  }
  if ((page === "genome" || page === "insights") && parts.length === 2) {
    const tab = decoded(parts[1]!);
    const known = page === "genome" ? GENOME_TABS : INSIGHT_TABS;
    return tab && known.has(tab) ? { page, tab } : { page: "not-found", requested };
  }
  if (parts.length === 1 && PAGE_SET.has(page)) return { page: page as PageId };
  return { page: "not-found", requested };
}

export function hrefFor(route: Route): string {
  if (route.page === "not-found") return `#${route.requested}`;
  if (route.page === "project-created") {
    return `#/project-created/${encodeURIComponent(route.workspace)}`;
  }
  if (route.page === "tasks" && "taskId" in route) {
    return `#/tasks/${encodeURIComponent(route.taskId)}`;
  }
  if (route.page === "requirements" && "requirementId" in route) {
    return `#/requirements/${encodeURIComponent(route.requirementId)}`;
  }
  if ("tab" in route && route.tab) return `#/${route.page}/${encodeURIComponent(route.tab)}`;
  return `#/${route.page}`;
}

export function routeForPage(page: PageId): Route {
  return { page };
}

export function navigate(route: Route, replace = false): void {
  const href = hrefFor(route);
  if (replace) window.history.replaceState(null, "", href);
  else window.history.pushState(null, "", href);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}
