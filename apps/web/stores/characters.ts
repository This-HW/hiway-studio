// Character Lab screen store (app/projects/[id]/characters).
// Master portrait generation is async (202 + SSE); completion is handled by
// the page watching the workspace job state.
"use client";

import { create } from "zustand";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiUpload,
} from "@/lib/api-client";
import type {
  AssetUploadResponse,
  Character,
  CharacterScope,
  JobStarted,
  ModelInfo,
  Reference,
  WardrobeItem,
} from "@/lib/types";
import { koreanErrorMessage } from "@/lib/format";
import { ApiError } from "@/lib/errors";
import { toast } from "./toast";

export type DetailTab = "references" | "wardrobe";

interface NewCharacterForm {
  name: string;
  description: string;
  appearance_prompt: string;
  consistency_method: string;
}

interface CharactersState {
  projectId: string | null;
  scope: CharacterScope;
  characters: Character[];
  loading: boolean;
  error: string | null;
  selectedId: string | null;
  detail: Character | null;
  detailTab: DetailTab;
  references: Reference[];
  wardrobe: WardrobeItem[];
  detailLoading: boolean;

  dialogOpen: boolean;
  creating: boolean;
  form: NewCharacterForm;

  // edit appearance (version bump confirmation flow)
  editOpen: boolean;
  editAppearancePrompt: string;
  editDescription: string;
  savingEdit: boolean;

  // master generation
  imageModels: ModelInfo[];
  masterModelId: string;
  masterPrompt: string;
  masterGenerating: boolean;
  masterJobByCharacter: Record<string, string>;
  uploadingMaster: boolean;

  // wardrobe add
  wardrobeName: string;
  wardrobePromptFragment: string;
  addingWardrobe: boolean;

  // delete
  deleteTarget: Character | null;
  deleting: boolean;

  init: (projectId: string) => Promise<void>;
  setScope: (s: CharacterScope) => void;
  loadCharacters: () => Promise<void>;
  select: (id: string | null) => Promise<void>;
  setDetailTab: (t: DetailTab) => void;
  refreshDetail: () => Promise<void>;
  refreshReferences: () => Promise<void>;
  loadImageModels: () => Promise<void>;

  openDialog: () => void;
  closeDialog: () => void;
  setForm: (patch: Partial<NewCharacterForm>) => void;
  createCharacter: () => Promise<void>;

  openEdit: () => void;
  closeEdit: () => void;
  setEditAppearancePrompt: (v: string) => void;
  setEditDescription: (v: string) => void;
  saveEdit: () => Promise<void>;

  setMasterModelId: (v: string) => void;
  setMasterPrompt: (v: string) => void;
  generateMaster: () => Promise<string | null>;
  markMasterJobDone: (characterId: string) => void;
  uploadMaster: (file: File) => Promise<void>;

  togglePrimary: (ref: Reference) => Promise<void>;
  deleteReference: (id: string) => Promise<void>;
  setWardrobeName: (v: string) => void;
  setWardrobePromptFragment: (v: string) => void;
  addWardrobe: () => Promise<void>;

  openDelete: (c: Character) => void;
  closeDelete: () => void;
  confirmDelete: () => Promise<void>;
}

const EMPTY_FORM: NewCharacterForm = {
  name: "",
  description: "",
  appearance_prompt: "",
  consistency_method: "hybrid",
};

export const useCharactersStore = create<CharactersState>((set, get) => ({
  projectId: null,
  scope: "project",
  characters: [],
  loading: false,
  error: null,
  selectedId: null,
  detail: null,
  detailTab: "references",
  references: [],
  wardrobe: [],
  detailLoading: false,

  dialogOpen: false,
  creating: false,
  form: EMPTY_FORM,

  editOpen: false,
  editAppearancePrompt: "",
  editDescription: "",
  savingEdit: false,

  imageModels: [],
  masterModelId: "",
  masterPrompt: "",
  masterGenerating: false,
  masterJobByCharacter: {},
  uploadingMaster: false,

  wardrobeName: "",
  wardrobePromptFragment: "",
  addingWardrobe: false,

  deleteTarget: null,
  deleting: false,

  init: async (projectId) => {
    set({
      projectId,
      loading: true,
      error: null,
      selectedId: null,
      detail: null,
      references: [],
      wardrobe: [],
      masterJobByCharacter: {},
    });
    await get().loadCharacters();
    set({ loading: false });
  },

  setScope: (s) => {
    set({ scope: s, selectedId: null, detail: null });
    void get().loadCharacters();
  },

  loadCharacters: async () => {
    const { projectId, scope } = get();
    if (!projectId) return;
    try {
      const chars = await apiGet<Character[]>(
        `/api/v1/projects/${projectId}/characters?scope=${scope}`
      );
      set({ characters: chars, error: null });
    } catch (e) {
      set({
        error:
          e instanceof Error ? e.message : "캐릭터 목록을 불러오지 못했습니다.",
      });
    }
  },

  select: async (id) => {
    set({
      selectedId: id,
      detail: null,
      references: [],
      wardrobe: [],
      detailTab: "references",
      detailLoading: id !== null,
    });
    if (id) await get().refreshDetail();
    set({ detailLoading: false });
  },

  setDetailTab: (t) => set({ detailTab: t }),

  refreshDetail: async () => {
    const { selectedId } = get();
    if (!selectedId) return;
    try {
      const detail = await apiGet<Character>(`/api/v1/characters/${selectedId}`);
      set({ detail });
      await Promise.all([get().refreshReferences(), (async () => {
        try {
          const w = await apiGet<WardrobeItem[]>(
            `/api/v1/characters/${selectedId}/wardrobe`
          );
          set({ wardrobe: w });
        } catch {
          // optional
        }
      })()]);
    } catch (e) {
      toast(e instanceof Error ? e.message : "캐릭터 상세를 불러오지 못했습니다.", {
        kind: "error",
      });
    }
  },

  refreshReferences: async () => {
    const { selectedId } = get();
    if (!selectedId) return;
    try {
      const refs = await apiGet<Reference[]>(
        `/api/v1/characters/${selectedId}/references`
      );
      set({ references: refs });
    } catch {
      // optional
    }
  },

  loadImageModels: async () => {
    try {
      const models = await apiGet<ModelInfo[]>(
        "/api/v1/models?modality=image"
      );
      set({ imageModels: models });
    } catch {
      // optional dropdown
    }
  },

  openDialog: () => set({ dialogOpen: true, form: EMPTY_FORM }),
  closeDialog: () => set({ dialogOpen: false }),
  setForm: (patch) => set((s) => ({ form: { ...s.form, ...patch } })),

  createCharacter: async () => {
    const { projectId, form } = get();
    if (!projectId) return;
    if (!form.name.trim()) {
      toast("캐릭터 이름을 입력해 주세요.", { kind: "error" });
      return;
    }
    // Backend CharacterCreate requires appearance_prompt (min_length=1).
    if (!form.appearance_prompt.trim()) {
      toast("외형 앵커를 입력해 주세요.", { kind: "error" });
      return;
    }
    set({ creating: true });
    try {
      await apiPost<Character>(`/api/v1/projects/${projectId}/characters`, {
        name: form.name.trim(),
        description: form.description.trim() || undefined,
        appearance_prompt: form.appearance_prompt.trim(),
        consistency_method: form.consistency_method || "hybrid",
      });
      set({ creating: false, dialogOpen: false, form: EMPTY_FORM });
      toast("캐릭터를 만들었습니다.");
      await get().loadCharacters();
    } catch (e) {
      set({ creating: false });
      toast(e instanceof Error ? e.message : "캐릭터 생성에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  openEdit: () => {
    const { detail } = get();
    set({
      editOpen: true,
      editAppearancePrompt: detail?.appearance_prompt ?? "",
      editDescription: detail?.description ?? "",
    });
  },
  closeEdit: () => set({ editOpen: false }),
  setEditAppearancePrompt: (v) => set({ editAppearancePrompt: v }),
  setEditDescription: (v) => set({ editDescription: v }),

  saveEdit: async () => {
    const { selectedId, editAppearancePrompt, editDescription } = get();
    if (!selectedId) return;
    set({ savingEdit: true });
    try {
      const updated = await apiPatch<Character>(
        `/api/v1/characters/${selectedId}`,
        {
          appearance_prompt: editAppearancePrompt,
          description: editDescription,
        }
      );
      set({
        detail: updated,
        savingEdit: false,
        editOpen: false,
        characters: get().characters.map((c) =>
          c.id === selectedId ? { ...c, ...updated } : c
        ),
      });
      toast(
        `외형이 변경되어 version ${updated.version}으로 증가했습니다. 이 캐릭터를 쓰는 샷에 "구식" 배지가 붙을 수 있습니다.`
      );
      await get().refreshDetail();
    } catch (e) {
      set({ savingEdit: false });
      toast(e instanceof Error ? e.message : "수정에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  setMasterModelId: (v) => set({ masterModelId: v }),
  setMasterPrompt: (v) => set({ masterPrompt: v }),

  generateMaster: async () => {
    const { selectedId, masterModelId, masterPrompt } = get();
    if (!selectedId || get().masterGenerating) return null;
    set({ masterGenerating: true });
    try {
      const res = await apiPost<JobStarted>(
        `/api/v1/characters/${selectedId}/master`,
        {
          prompt: masterPrompt.trim() || undefined,
          model_id: masterModelId || undefined,
        }
      );
      set((s) => ({
        masterGenerating: false,
        masterJobByCharacter: {
          ...s.masterJobByCharacter,
          [selectedId]: res.job_id,
        },
      }));
      toast("마스터 초상 생성을 시작했습니다. 완료되면 썸네일이 갱신됩니다.");
      return res.job_id;
    } catch (e) {
      set({ masterGenerating: false });
      const msg =
        e instanceof ApiError
          ? koreanErrorMessage(e.code, e.message)
          : "마스터 생성 요청에 실패했습니다.";
      toast(msg, { kind: "error" });
      return null;
    }
  },

  markMasterJobDone: (characterId) =>
    set((s) => {
      const next = { ...s.masterJobByCharacter };
      delete next[characterId];
      return { masterJobByCharacter: next };
    }),

  uploadMaster: async (file) => {
    const { projectId, selectedId } = get();
    if (!projectId || !selectedId || get().uploadingMaster) return;
    set({ uploadingMaster: true });
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("project_id", projectId);
      // Uploaded as a character reference (asset_kind "reference").
      form.append("kind", "reference");
      const { path } = await apiUpload<AssetUploadResponse>(
        "/api/v1/assets/upload",
        form
      );
      await apiPost<Reference>(`/api/v1/characters/${selectedId}/references`, {
        image_path: path,
        role: "master",
        source: "uploaded",
        is_primary: true,
      });
      toast("마스터 이미지를 업로드하고 레퍼런스로 등록했습니다.");
      await get().refreshDetail();
      await get().loadCharacters();
    } catch (e) {
      toast(e instanceof Error ? e.message : "업로드에 실패했습니다.", {
        kind: "error",
      });
    } finally {
      set({ uploadingMaster: false });
    }
  },

  togglePrimary: async (ref) => {
    try {
      await apiPatch<Reference>(`/api/v1/references/${ref.id}`, {
        is_primary: !ref.is_primary,
      });
      toast(
        ref.is_primary
          ? "주 레퍼런스 지정을 해제했습니다."
          : "주 레퍼런스로 지정했습니다."
      );
      await get().refreshReferences();
    } catch (e) {
      toast(e instanceof Error ? e.message : "지정 변경에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  deleteReference: async (id) => {
    try {
      await apiDelete<unknown>(`/api/v1/references/${id}`);
      toast("레퍼런스를 삭제했습니다.");
      await get().refreshReferences();
    } catch (e) {
      toast(e instanceof Error ? e.message : "삭제에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  setWardrobeName: (v) => set({ wardrobeName: v }),
  setWardrobePromptFragment: (v) => set({ wardrobePromptFragment: v }),

  addWardrobe: async () => {
    const { selectedId, wardrobeName, wardrobePromptFragment } = get();
    if (!selectedId) return;
    if (!wardrobeName.trim()) {
      toast("의상 이름을 입력해 주세요.", { kind: "error" });
      return;
    }
    if (!wardrobePromptFragment.trim()) {
      toast("프롬프트 조각을 입력해 주세요.", { kind: "error" });
      return;
    }
    set({ addingWardrobe: true });
    try {
      await apiPost<WardrobeItem>(
        `/api/v1/characters/${selectedId}/wardrobe`,
        {
          name: wardrobeName.trim(),
          prompt_fragment: wardrobePromptFragment.trim(),
        }
      );
      set({
        wardrobeName: "",
        wardrobePromptFragment: "",
        addingWardrobe: false,
      });
      toast("의상을 추가했습니다.");
      const w = await apiGet<WardrobeItem[]>(
        `/api/v1/characters/${selectedId}/wardrobe`
      );
      set({ wardrobe: w });
    } catch (e) {
      set({ addingWardrobe: false });
      toast(e instanceof Error ? e.message : "의상 추가에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  openDelete: (c) => set({ deleteTarget: c }),
  closeDelete: () => set({ deleteTarget: null }),

  confirmDelete: async () => {
    const { deleteTarget, selectedId } = get();
    if (!deleteTarget) return;
    set({ deleting: true });
    try {
      await apiDelete<unknown>(`/api/v1/characters/${deleteTarget.id}`);
      toast(`캐릭터 "${deleteTarget.name}"을(를) 삭제했습니다.`);
      set({ deleting: false, deleteTarget: null });
      if (selectedId === deleteTarget.id) {
        await get().select(null);
      }
      await get().loadCharacters();
    } catch (e) {
      set({ deleting: false });
      toast(e instanceof Error ? e.message : "삭제에 실패했습니다.", {
        kind: "error",
      });
    }
  },
}));
