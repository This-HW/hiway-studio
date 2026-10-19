// Project list screen store (app/projects).
"use client";

import { create } from "zustand";
import { apiDelete, apiGet, apiPost } from "@/lib/api-client";
import type { CategoryPack, DefaultModels, ModelInfo, Project } from "@/lib/types";
import { toast } from "./toast";

interface NewProjectForm {
  title: string;
  aspect_ratio: string;
  category_pack_code: string;
  text_model_id: string;
  image_model_id: string;
}

interface ProjectsState {
  projects: Project[];
  loading: boolean;
  error: string | null;
  showArchived: boolean;
  dialogOpen: boolean;
  creating: boolean;
  form: NewProjectForm;
  categoryPacks: CategoryPack[];
  textModels: ModelInfo[];
  imageModels: ModelInfo[];
  // delete confirmation
  deleteTarget: Project | null;
  deleteTitleInput: string;
  deleting: boolean;

  load: (archived: boolean) => Promise<void>;
  setShowArchived: (v: boolean) => void;
  openDialog: () => Promise<void>;
  closeDialog: () => void;
  setForm: (patch: Partial<NewProjectForm>) => void;
  createProject: () => Promise<string | null>;
  duplicate: (id: string) => Promise<void>;
  archive: (id: string) => Promise<void>;
  openDelete: (p: Project) => void;
  closeDelete: () => void;
  setDeleteTitleInput: (v: string) => void;
  confirmDelete: () => Promise<void>;
}

const EMPTY_FORM: NewProjectForm = {
  title: "",
  aspect_ratio: "16:9",
  category_pack_code: "",
  text_model_id: "",
  image_model_id: "",
};

export const useProjectsStore = create<ProjectsState>((set, get) => ({
  projects: [],
  loading: false,
  error: null,
  showArchived: false,
  dialogOpen: false,
  creating: false,
  form: EMPTY_FORM,
  categoryPacks: [],
  textModels: [],
  imageModels: [],
  deleteTarget: null,
  deleteTitleInput: "",
  deleting: false,

  load: async (archived) => {
    set({ loading: true, error: null });
    try {
      const data = await apiGet<Project[]>(
        `/api/v1/projects?is_archived=${archived ? "true" : "false"}`
      );
      set({ projects: data, loading: false });
    } catch (e) {
      set({
        loading: false,
        error: e instanceof Error ? e.message : "프로젝트 목록을 불러오지 못했습니다.",
      });
    }
  },

  setShowArchived: (v) => {
    set({ showArchived: v });
    void get().load(v);
  },

  openDialog: async () => {
    set({ dialogOpen: true, form: EMPTY_FORM });
    try {
      const [packs, textModels, imageModels] = await Promise.all([
        apiGet<CategoryPack[]>("/api/v1/category-packs"),
        apiGet<ModelInfo[]>("/api/v1/models?modality=text"),
        apiGet<ModelInfo[]>("/api/v1/models?modality=image"),
      ]);
      set({ categoryPacks: packs, textModels, imageModels });
    } catch {
      // Dropdowns stay empty; creation still works without optional fields.
    }
  },

  closeDialog: () => set({ dialogOpen: false }),

  setForm: (patch) => set((s) => ({ form: { ...s.form, ...patch } })),

  createProject: async () => {
    const { form, showArchived } = get();
    if (!form.title.trim()) {
      toast("프로젝트 제목을 입력해 주세요.", { kind: "error" });
      return null;
    }
    set({ creating: true });
    try {
      const default_models: DefaultModels = {};
      if (form.text_model_id) default_models.text_model_id = form.text_model_id;
      if (form.image_model_id) default_models.image_model_id = form.image_model_id;
      const project = await apiPost<Project>("/api/v1/projects", {
        title: form.title.trim(),
        aspect_ratio: form.aspect_ratio,
        category_pack_code: form.category_pack_code || undefined,
        default_models: Object.keys(default_models).length > 0 ? default_models : undefined,
      });
      set({ creating: false, dialogOpen: false, form: EMPTY_FORM });
      toast(`프로젝트 "${project.title}"을(를) 만들었습니다.`);
      await get().load(showArchived);
      return project.id;
    } catch (e) {
      set({ creating: false });
      toast(e instanceof Error ? e.message : "프로젝트 생성에 실패했습니다.", {
        kind: "error",
      });
      return null;
    }
  },

  duplicate: async (id) => {
    try {
      const project = await apiPost<Project>(
        `/api/v1/projects/${id}/duplicate`,
        {}
      );
      toast(`프로젝트 "${project.title}"을(를) 복제했습니다.`);
      await get().load(get().showArchived);
    } catch (e) {
      toast(e instanceof Error ? e.message : "복제에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  archive: async (id) => {
    try {
      await apiPost<{ job_id: string }>(
        `/api/v1/projects/${id}/archive`,
        {}
      );
      toast("아카이브를 생성 중입니다. 진행률은 상단 잡 뱃지에서 확인하세요.");
      await get().load(get().showArchived);
    } catch (e) {
      toast(e instanceof Error ? e.message : "아카이브 요청에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  openDelete: (p) => set({ deleteTarget: p, deleteTitleInput: "" }),
  closeDelete: () => set({ deleteTarget: null, deleteTitleInput: "" }),
  setDeleteTitleInput: (v) => set({ deleteTitleInput: v }),

  confirmDelete: async () => {
    const { deleteTarget, deleteTitleInput, showArchived } = get();
    if (!deleteTarget) return;
    if (deleteTitleInput.trim() !== deleteTarget.title) {
      toast("제목이 일치하지 않습니다.", { kind: "error" });
      return;
    }
    set({ deleting: true });
    try {
      await apiDelete<unknown>(
        `/api/v1/projects/${deleteTarget.id}?confirm=true`
      );
      toast(`프로젝트 "${deleteTarget.title}"을(를) 삭제했습니다.`);
      set({ deleting: false, deleteTarget: null, deleteTitleInput: "" });
      await get().load(showArchived);
    } catch (e) {
      set({ deleting: false });
      toast(e instanceof Error ? e.message : "삭제에 실패했습니다.", {
        kind: "error",
      });
    }
  },
}));
