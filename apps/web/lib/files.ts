// File URL resolution.
//
// Backend read models expose `image_path` (a path relative to the outputs
// root, e.g. `library/assets/<uuid>.png`), not a URL. The backend serves
// those paths statically at GET /files/{image_path} (design §5, local-only),
// so this maps image_path -> served URL. Single conversion point: every
// image slot in the UI goes through here.
import { API_BASE } from "./api-client";

export function resolveFileUrl(
  imagePath: string | null | undefined
): string | null {
  if (!imagePath) return null;
  const rel = imagePath.replace(/^\/+/, "");
  if (rel.startsWith("..") || rel.includes("/../") || rel.endsWith("/..")) {
    return null; // never emit a traversal URL
  }
  return `${API_BASE}/files/${rel}`;
}
