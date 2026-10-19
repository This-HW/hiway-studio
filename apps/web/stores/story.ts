// Story/plot screen store (app/projects/[id]/story).
// LLM generate calls are synchronous (201); results enter as drafts until
// the user confirms them with PUT /projects/{id}/story.
"use client";

import { create } from "zustand";
import { apiDelete, apiGet, apiPatch, apiPost, apiPut, apiUpload } from "@/lib/api-client";
import type {
  Beat,
  GenerateBeatsResponse,
  GenerateSynopsisResponse,
  ImportScriptResponse,
  ModelInfo,
  Story,
} from "@/lib/types";
import { koreanErrorMessage } from "@/lib/format";
import { ApiError } from "@/lib/errors";
import { toast } from "./toast";

interface StoryState {
  projectId: string | null;
  story: Story | null;
  beats: Beat[];
  selectedBeatId: string | null;
  loading: boolean;
  error: string | null;
  saving: boolean;
  dirty: boolean;

  // LLM draft state (synopsis only; beats are saved server-side immediately)
  llmModelId: string;
  textModels: ModelInfo[];
  generatingSynopsis: boolean;
  generatingBeats: boolean;
  synopsisDraft: string | null;

  // beat editor
  editingBeatId: string | null;
  beatFormTitle: string;
  beatFormSummary: string;

  // import
  importing: boolean;

  init: (projectId: string) => Promise<void>;
  loadModels: () => Promise<void>;
  setLogline: (v: string) => void;
  setSynopsis: (v: string) => void;
  setSynopsisDraft: (v: string) => void;
  setLlmModelId: (v: string) => void;
  generateSynopsis: () => Promise<void>;
  discardSynopsisDraft: () => void;
  generateBeats: () => Promise<void>;
  saveStory: () => Promise<void>;
  refreshBeats: () => Promise<void>;
  selectBeat: (id: string | null) => void;
  startBeatEdit: (b: Beat) => void;
  cancelBeatEdit: () => void;
  setBeatForm: (title: string, summary: string) => void;
  addBeat: () => Promise<void>;
  updateBeat: () => Promise<void>;
  deleteBeat: (id: string) => Promise<void>;
  importScript: (file: File, format: "fountain" | "fdx" | "txt") => Promise<void>;
}

export const useStoryStore = create<StoryState>((set, get) => ({
  projectId: null,
  story: null,
  beats: [],
  selectedBeatId: null,
  loading: false,
  error: null,
  saving: false,
  dirty: false,

  llmModelId: "",
  textModels: [],
  generatingSynopsis: false,
  generatingBeats: false,
  synopsisDraft: null,

  editingBeatId: null,
  beatFormTitle: "",
  beatFormSummary: "",

  importing: false,

  init: async (projectId) => {
    set({
      projectId,
      loading: true,
      error: null,
      story: null,
      beats: [],
      selectedBeatId: null,
      synopsisDraft: null,
      dirty: false,
      editingBeatId: null,
    });
    try {
      const raw = await apiGet<Story>(`/api/v1/projects/${projectId}/story`);
      const story: Story = {
        ...raw,
        logline: raw.logline ?? "",
        synopsis: raw.synopsis ?? "",
      };
      set({ story, loading: false });
      await get().refreshBeats();
    } catch (e) {
      set({
        loading: false,
        error: e instanceof Error ? e.message : "스토리를 불러오지 못했습니다.",
      });
    }
  },

  loadModels: async () => {
    try {
      const models = await apiGet<ModelInfo[]>("/api/v1/models?modality=text");
      set({ textModels: models });
    } catch {
      // optional dropdown; ignore
    }
  },

  setLogline: (v) =>
    set((s) => ({
      story: s.story ? { ...s.story, logline: v } : s.story,
      dirty: true,
    })),

  setSynopsis: (v) =>
    set((s) => ({
      story: s.story ? { ...s.story, synopsis: v } : s.story,
      dirty: true,
    })),

  setSynopsisDraft: (v) => set({ synopsisDraft: v, dirty: true }),

  setLlmModelId: (v) => set({ llmModelId: v }),

  generateSynopsis: async () => {
    const { projectId, llmModelId, story } = get();
    if (!projectId || get().generatingSynopsis) return;
    const logline = story?.logline?.trim() ?? "";
    if (!logline) {
      toast("로그라인을 먼저 입력해 주세요.", { kind: "error" });
      return;
    }
    set({ generatingSynopsis: true });
    try {
      const res = await apiPost<GenerateSynopsisResponse>(
        `/api/v1/projects/${projectId}/story/generate-synopsis`,
        { model_id: llmModelId || undefined, logline }
      );
      // Synchronous 201: the result enters the editor as a draft.
      set({ synopsisDraft: res.synopsis, generatingSynopsis: false });
      toast("시놉시스 초안이 생성되었습니다. 확정 버튼을 눌러 저장하세요.");
    } catch (e) {
      set({ generatingSynopsis: false });
      const msg =
        e instanceof ApiError
          ? koreanErrorMessage(e.code, e.message)
          : "시놉시스 생성에 실패했습니다.";
      toast(msg, { kind: "error" });
    }
  },

  discardSynopsisDraft: () => set({ synopsisDraft: null }),

  generateBeats: async () => {
    const { projectId, llmModelId } = get();
    if (!projectId || get().generatingBeats) return;
    set({ generatingBeats: true });
    try {
      const res = await apiPost<GenerateBeatsResponse>(
        `/api/v1/projects/${projectId}/story/generate-beats`,
        { model_id: llmModelId || undefined }
      );
      // Synchronous 201: the server already saved the beats.
      const normalized = res.beats.map((b) => ({
        ...b,
        summary: b.summary ?? "",
      }));
      set({ beats: normalized.sort((a, b) => a.order_index - b.order_index) });
      toast(`${res.beats.length}개의 비트가 생성·저장되었습니다.`);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? koreanErrorMessage(e.code, e.message)
          : "비트 생성에 실패했습니다.";
      toast(msg, { kind: "error" });
    } finally {
      set({ generatingBeats: false });
    }
  },

  saveStory: async () => {
    const { projectId, story, synopsisDraft } = get();
    if (!projectId || !story || get().saving) return;
    set({ saving: true });
    try {
      const savedRaw = await apiPut<Story>(`/api/v1/projects/${projectId}/story`, {
        logline: story.logline,
        synopsis: synopsisDraft ?? story.synopsis,
      });
      const saved: Story = {
        ...savedRaw,
        logline: savedRaw.logline ?? "",
        synopsis: savedRaw.synopsis ?? "",
      };
      set({ story: saved, synopsisDraft: null, saving: false, dirty: false });
      toast("스토리를 저장했습니다.");
    } catch (e) {
      set({ saving: false });
      toast(e instanceof Error ? e.message : "저장에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  refreshBeats: async () => {
    const { projectId, story } = get();
    const storyId = story?.id;
    if (!projectId || !storyId) return;
    try {
      const beats = await apiGet<Beat[]>(
        `/api/v1/stories/${storyId}/beats`
      );
      const normalized = beats.map((b) => ({ ...b, summary: b.summary ?? "" }));
      set({
        beats: normalized.sort((a, b) => a.order_index - b.order_index),
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : "비트 목록을 불러오지 못했습니다.", {
        kind: "error",
      });
    }
  },

  selectBeat: (id) => set({ selectedBeatId: id }),

  startBeatEdit: (b) =>
    set({
      editingBeatId: b.id,
      beatFormTitle: b.title ?? "",
      beatFormSummary: b.summary ?? "",
    }),

  cancelBeatEdit: () =>
    set({ editingBeatId: null, beatFormTitle: "", beatFormSummary: "" }),

  setBeatForm: (title, summary) =>
    set({ beatFormTitle: title, beatFormSummary: summary }),

  addBeat: async () => {
    const { story, beatFormTitle, beatFormSummary } = get();
    if (!story || !beatFormSummary.trim()) {
      toast("비트 요약을 입력해 주세요.", { kind: "error" });
      return;
    }
    try {
      const created = await apiPost<Beat>(`/api/v1/stories/${story.id}/beats`, {
        title: beatFormTitle.trim() || undefined,
        summary: beatFormSummary.trim(),
      });
      const beat: Beat = { ...created, summary: created.summary ?? "" };
      set((s) => ({
        beats: [...s.beats, beat].sort((a, b) => a.order_index - b.order_index),
        beatFormTitle: "",
        beatFormSummary: "",
      }));
      toast("비트를 추가했습니다.");
    } catch (e) {
      toast(e instanceof Error ? e.message : "비트 추가에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  updateBeat: async () => {
    const { editingBeatId, beatFormTitle, beatFormSummary } = get();
    if (!editingBeatId || !beatFormSummary.trim()) {
      toast("비트 요약을 입력해 주세요.", { kind: "error" });
      return;
    }
    try {
      const patched = await apiPatch<Beat>(`/api/v1/beats/${editingBeatId}`, {
        title: beatFormTitle.trim() || null,
        summary: beatFormSummary.trim(),
      });
      const updated: Beat = { ...patched, summary: patched.summary ?? "" };
      set((s) => ({
        beats: s.beats
          .map((b) => (b.id === editingBeatId ? updated : b))
          .sort((a, b) => a.order_index - b.order_index),
        editingBeatId: null,
        beatFormTitle: "",
        beatFormSummary: "",
      }));
      toast("비트를 수정했습니다.");
    } catch (e) {
      toast(e instanceof Error ? e.message : "비트 수정에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  deleteBeat: async (id) => {
    try {
      await apiDelete<unknown>(`/api/v1/beats/${id}`);
      set((s) => ({
        beats: s.beats.filter((b) => b.id !== id),
        selectedBeatId: s.selectedBeatId === id ? null : s.selectedBeatId,
        editingBeatId: s.editingBeatId === id ? null : s.editingBeatId,
      }));
      toast("비트를 삭제했습니다. (연결된 씬은 유지되며 beat 연결만 해제됩니다)");
    } catch (e) {
      toast(e instanceof Error ? e.message : "비트 삭제에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  importScript: async (file, format) => {
    const { projectId } = get();
    if (!projectId || get().importing) return;
    set({ importing: true });
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("format", format);
      const res = await apiUpload<ImportScriptResponse>(
        `/api/v1/projects/${projectId}/story/import`,
        form
      );
      toast(`${res.scenes_created}개의 씬이 생성되었습니다.`, {
        actionLabel: "보드에서 확인",
        onAction: () => {
          window.location.href = `/projects/${projectId}/board`;
        },
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : "스크립트 임포트에 실패했습니다.", {
        kind: "error",
      });
    } finally {
      set({ importing: false });
    }
  },
}));
