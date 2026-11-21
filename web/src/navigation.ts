export const runLink = (id: string) => `#/runs/${encodeURIComponent(id)}`;
export const recordLink = (id: string, key: string) =>
  `${runLink(id)}/record/${key.split("/").map(encodeURIComponent).join("/")}`;
export const compareLink = (left: string, right: string) =>
  `#/compare/${encodeURIComponent(left)}/${encodeURIComponent(right)}`;

export function go(path: string) {
  window.location.hash = path;
}
