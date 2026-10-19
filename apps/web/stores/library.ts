// Global library screen store (app/library).
// Listing endpoints are project-scoped, so the screen keeps a reference
// project to list assets / global characters against. Uploads can target
// the global library (project_id omitted) or a specific project.
"use client";

import { create } from "zustand";
import { apiDelete, apiGet, apiUpload } from "@/lib/api-client";
import type { Asset, AssetKind, Character, Project } from "@/lib/types";
import { toast } from "./toast";

export type LibraryTab = "assets" | "characters";

const ALLOWED_EXT = ["png", "jpg", "jpeg", "webp", "mp4", "wav", "mp3"];
const MAX_BYTES = 20 * 1024 * 1024;

interface LibraryState {
  projects: Project[];
  projectId: string | null;
  tab: LibraryTab;
  kindFilter: string; // "all" | AssetKind
  assets: Asset[];
  globalCharacters: Character[];
  loading: boolean;
  uploading: boolean;
  uploadKind: AssetKind;
  uploadTarget: string; // "global" | projectId

  init: () => Promise<void>;
  setProjectId: (id: string) => void;
  setTab: (t: LibraryTab) => void;
  setKindFilter: (k: string) => void;
  setUploadKind: (k: AssetKind) => void;
  setUploadTarget: (t: string) => void;
  load: () => Promise<void>;
  upload: (file: File) => Promise<void>;
  deleteAsset: (a: Asset) => Promise<void>;
}

function validateFile(file: File): string | null {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  if (!ALLOWED_EXT.includes(ext)) {
    return `지원하지 않는 파일 형식입니다: .${ext} (png/jpg/webp/mp4/wav/mp3만 가능)`;
  }
  if (file.size > MAX_BYTES) {
    return "파일이 20MB 제한을 초과했습니다.";
  }
  return null;
}

export const useLibraryStore = create<LibraryState>((set, get) => ({
  projects: [],
  projectId: null,
  tab: "assets",
  kindFilter: "all",
  assets: [],
  globalCharacters: [],
  loading: false,
  uploading: false,
  uploadKind: "background",
  uploadTarget: "global",

  init: async () => {
    set({ loading: true });
    try {
      const projects = await apiGet<Project[]>("/api/v1/projects?is_archived=false");
      const projectId = projects[0]?.id ?? null;
      set({ projects, projectId, uploadTarget: "global" });
      if (projectId) await get().load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "프로젝트 목록을 불러오지 못했습니다.", {
        kind: "error",
      });
    } finally {
      set({ loading: false });
    }
  },

  setProjectId: (id) => {
    set({ projectId: id });
    void get().load();
  },

  setTab: (t) => set({ tab: t }),
  setKindFilter: (k) => {
    set({ kindFilter: k });
    void get().load();
  },
  setUploadKind: (k) => set({ uploadKind: k }),
  setUploadTarget: (t) => set({ uploadTarget: t }),

  load: async () => {
    const { projectId, kindFilter, tab } = get();
    if (!projectId) return;
    try {
      if (tab === "assets") {
        const query =
          kindFilter === "all"
            ? ""
            : `?kind=${encodeURIComponent(kindFilter)}`;
        const assets = await apiGet<Asset[]>(
          `/api/v1/projects/${projectId}/assets${query}`
        );
        set({ assets });
      } else {
        const chars = await apiGet<Character[]>(
          `/api/v1/projects/${projectId}/characters?scope=global`
        );
        set({ globalCharacters: chars });
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : "라이브러리를 불러오지 못했습니다.", {
        kind: "error",
      });
    }
  },

  upload: async (file) => {
    const { uploadKind, uploadTarget } = get();
    if (get().uploading) return;
    const err = validateFile(file);
    if (err) {
      toast(err, { kind: "error" });
      return;
    }
    set({ uploading: true });
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", uploadKind);
      if (uploadTarget !== "global") form.append("project_id", uploadTarget);
      await apiUpload<Asset>("/api/v1/assets/upload", form);
      toast(
        uploadTarget === "global"
          ? "전역 라이브러리에 업로드했습니다."
          : "프로젝트에 업로드했습니다."
      );
      if (get().tab === "assets") await get().load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "업로드에 실패했습니다.", {
        kind: "error",
      });
    } finally {
      set({ uploading: false });
    }
  },

  deleteAsset: async (a) => {
    const referencing = a.referencing_shots ?? 0;
    if (referencing > 0) {
      const ok = window.confirm(
        `이 에셋을 참조하는 샷이 ${referencing}개 있습니다. 삭제하면 해당 샷의 레퍼런스에서 제거됩니다. 정말 삭제할까요?`
      );
      if (!ok) return;
    } else {
      const ok = window.confirm(`"${a.name}" 에셋을 삭제할까요?`);
      if (!ok) return;
    }
    try {
      await apiDelete<unknown>(`/api/v1/assets/${a.id}`);
      toast("에셋을 삭제했습니다.");
      await get().load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "삭제에 실패했습니다.", {
        kind: "error",
      });
    }
  },
}));
