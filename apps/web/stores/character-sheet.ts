// Character sheet screen store (app/projects/[id]/characters/[cid]/sheet).
// Sheet generation is async (202 {job_ids[]}); each job.completed refills
// the section grids via a references refetch.
"use client";

import { create } from "zustand";
import { apiGet, apiPatch, apiPost } from "@/lib/api-client";
import type { Character, Reference, SheetStarted } from "@/lib/types";
import { formatEstimatedCost, koreanErrorMessage } from "@/lib/format";
import { ApiError } from "@/lib/errors";
import { toast } from "./toast";

export interface SheetOptions {
  views: boolean;
  expressions: boolean;
  outfits: boolean;
  poses: boolean;
}

export interface SheetSection {
  key: string;
  title: string;
  roles: string[];
  items: Reference[];
}

/** Exact backend role lists, per packages/contracts reference_role. */
export const SHEET_ROLE_LISTS: Record<keyof SheetOptions, string[]> = {
  views: [
    "turnaround_front",
    "turnaround_three_quarter",
    "turnaround_profile",
    "turnaround_back",
  ],
  expressions: [
    "expression_neutral",
    "expression_happy",
    "expression_sad",
    "expression_angry",
    "expression_surprised",
    "expression_determined",
    "expression_vulnerable",
  ],
  outfits: ["outfit_default", "outfit_alt"],
  poses: ["pose_action"],
};

const DETAIL_ROLES = ["detail_face", "detail_hands", "detail_prop"];

/** Map backend reference roles onto the five display sections. */
export function groupReferencesBySection(refs: Reference[]): SheetSection[] {
  const sections: SheetSection[] = [
    { key: "views", title: "턴어라운드", roles: SHEET_ROLE_LISTS.views, items: [] },
    { key: "expressions", title: "표정", roles: SHEET_ROLE_LISTS.expressions, items: [] },
    { key: "outfits", title: "의상", roles: SHEET_ROLE_LISTS.outfits, items: [] },
    { key: "poses", title: "포즈", roles: SHEET_ROLE_LISTS.poses, items: [] },
    { key: "details", title: "디테일", roles: DETAIL_ROLES, items: [] },
    { key: "other", title: "기타", roles: [], items: [] },
  ];
  for (const ref of refs) {
    const role = (ref.role ?? "").toLowerCase();
    const section =
      sections.find((s) => s.roles.includes(role)) ??
      sections[sections.length - 1];
    section.items.push(ref);
  }
  return sections;
}

interface CharacterSheetState {
  characterId: string | null;
  character: Character | null;
  references: Reference[];
  loading: boolean;

  options: SheetOptions;
  generating: boolean;
  pendingJobIds: string[];
  doneJobIds: string[];
  failedJobIds: string[];
  estimatedCost: string | null;

  init: (characterId: string) => Promise<void>;
  setOption: (key: keyof SheetOptions, v: boolean) => void;
  generateSheet: () => Promise<void>;
  /** Called by the page when a workspace-tracked job finishes/fails. */
  markJobDone: (jobId: string, failed: boolean) => void;
  togglePrimary: (ref: Reference) => Promise<void>;
  refresh: () => Promise<void>;
}

export const useCharacterSheetStore = create<CharacterSheetState>((set, get) => ({
  characterId: null,
  character: null,
  references: [],
  loading: false,

  options: { views: true, expressions: true, outfits: true, poses: true },
  generating: false,
  pendingJobIds: [],
  doneJobIds: [],
  failedJobIds: [],
  estimatedCost: null,

  init: async (characterId) => {
    set({
      characterId,
      loading: true,
      character: null,
      references: [],
      pendingJobIds: [],
      doneJobIds: [],
      failedJobIds: [],
      estimatedCost: null,
    });
    try {
      const [character, references] = await Promise.all([
        apiGet<Character>(`/api/v1/characters/${characterId}`),
        apiGet<Reference[]>(`/api/v1/characters/${characterId}/references`),
      ]);
      set({ character, references, loading: false });
    } catch (e) {
      set({ loading: false });
      toast(
        e instanceof Error ? e.message : "캐릭터 시트를 불러오지 못했습니다.",
        { kind: "error" }
      );
    }
  },

  setOption: (key, v) => set((s) => ({ options: { ...s.options, [key]: v } })),

  generateSheet: async () => {
    const { characterId, options } = get();
    if (!characterId || get().generating) return;
    const selected = Object.entries(options)
      .filter(([, v]) => v)
      .map(([k]) => k);
    if (selected.length === 0) {
      toast("생성할 섹션을 하나 이상 선택해 주세요.", { kind: "error" });
      return;
    }
    set({ generating: true });
    try {
      // Backend SheetGenerateRequest takes explicit role lists (None = all).
      // Selected section -> explicit role list; deselected -> [] (none).
      const body: Record<string, string[]> = {
        views: options.views ? SHEET_ROLE_LISTS.views : [],
        expressions: options.expressions ? SHEET_ROLE_LISTS.expressions : [],
        outfits: options.outfits ? SHEET_ROLE_LISTS.outfits : [],
        poses: options.poses ? SHEET_ROLE_LISTS.poses : [],
      };
      const res = await apiPost<SheetStarted>(
        `/api/v1/characters/${characterId}/sheet`,
        body
      );
      set({
        generating: false,
        pendingJobIds: res.job_ids,
        doneJobIds: [],
        failedJobIds: [],
        estimatedCost: res.estimated_cost_usd ?? null,
      });
      if (res.estimated_cost_usd !== undefined) {
        toast(
          `시트 생성을 시작했습니다 (${formatEstimatedCost(
            res.estimated_cost_usd
          )}).`
        );
      } else {
        toast("시트 생성을 시작했습니다. 각 잡이 완료될 때마다 이미지가 채워집니다.");
      }
    } catch (e) {
      set({ generating: false });
      const msg =
        e instanceof ApiError
          ? koreanErrorMessage(e.code, e.message)
          : "시트 생성 요청에 실패했습니다.";
      toast(msg, { kind: "error" });
    }
  },

  markJobDone: (jobId, failed) => {
    const { pendingJobIds } = get();
    if (!pendingJobIds.includes(jobId)) return;
    set((s) => ({
      pendingJobIds: s.pendingJobIds.filter((j) => j !== jobId),
      doneJobIds: failed ? s.doneJobIds : [...s.doneJobIds, jobId],
      failedJobIds: failed ? [...s.failedJobIds, jobId] : s.failedJobIds,
    }));
    // Refill the grids from the latest references.
    void get().refresh();
  },

  togglePrimary: async (ref) => {
    try {
      await apiPatch<Reference>(`/api/v1/references/${ref.id}`, {
        is_primary: !ref.is_primary,
      });
      toast(
        ref.is_primary
          ? "주 레퍼런스 지정을 해제했습니다."
          : "주 레퍼런스로 지정했습니다. 샷 생성 시 자동 주입됩니다."
      );
      await get().refresh();
    } catch (e) {
      toast(e instanceof Error ? e.message : "지정 변경에 실패했습니다.", {
        kind: "error",
      });
    }
  },

  refresh: async () => {
    const { characterId } = get();
    if (!characterId) return;
    try {
      const references = await apiGet<Reference[]>(
        `/api/v1/characters/${characterId}/references`
      );
      set({ references });
    } catch {
      // non-fatal
    }
  },
}));
