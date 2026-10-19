// React Query keys. Extended for Phase 1 screens.
export const queryKeys = {
  health: ["health"] as const,
  projects: (archived: boolean) => ["projects", archived] as const,
  project: (id: string) => ["project", id] as const,
  categoryPacks: ["category-packs"] as const,
  models: (modality: string) => ["models", modality] as const,
  story: (projectId: string) => ["story", projectId] as const,
  beats: (storyId: string) => ["beats", storyId] as const,
  characters: (projectId: string, scope: string) =>
    ["characters", projectId, scope] as const,
  character: (id: string) => ["character", id] as const,
  references: (characterId: string) =>
    ["references", characterId] as const,
  wardrobe: (characterId: string) => ["wardrobe", characterId] as const,
  assets: (projectId: string, kind?: string) =>
    ["assets", projectId, kind ?? "all"] as const,
};
