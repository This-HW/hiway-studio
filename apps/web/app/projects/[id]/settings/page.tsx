// Project settings tab (P0-1, P0-19): title, budget, default models,
// aspect ratio, category pack, and style kit management (list/create/
// duplicate/lock). Locked kits cannot be edited directly — duplicate to
// create a new version (ADR-0011 §13).
"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPatch, apiPost } from "@/lib/api-client";
import { ApiError } from "@/lib/errors";
import type { CategoryPack, Project } from "@/lib/types";
import { useToastStore } from "@/stores/toast";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : "요청에 실패했습니다.";
}

interface Asset {
  id: string;
  name: string;
  kind: string;
  image_path: string | null;
}

// P1-7 스타일 추출 (§05C-5): 레퍼런스 이미지에서 스타일을 추출해 키트에 반영
function StyleExtractCard({ projectId, kits }: { projectId: string; kits: StyleKit[] }) {
  const [kitId, setKitId] = useState("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [warning, setWarning] = useState<string | null>(null);
  const [cost, setCost] = useState<string | null>(null);

  useEffect(() => {
    apiGet<Asset[]>(`/api/v1/projects/${projectId}/assets`)
      .then((a) => setAssets(a.filter((x) => x.kind === "image" || x.image_path)))
      .catch(() => setAssets([]));
  }, [projectId]);

  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const run = async () => {
    if (!kitId) return;
    setBusy(true);
    setError("");
    setWarning(null);
    try {
      const res = await apiPost<{ warning: string | null; estimated_cost_usd: string }>(
        `/api/v1/style-kits/${kitId}/extract`,
        { source_asset_ids: selected },
        { idempotencyKey: `style-extract-${kitId}-${Date.now()}` },
      );
      setWarning(res.warning);
      setCost(res.estimated_cost_usd);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" style={{ marginBottom: 16 }}>
      <div className="card-title">스타일 추출 (P1-7)</div>
      <p className="small muted" style={{ marginBottom: 12 }}>
        3~8장의 레퍼런스 이미지에서 스타일을 추출해 키트에 반영합니다. 3장 미만이어도 실행되지만 경고가 표시됩니다.
      </p>
      <div className="field">
        <label>대상 키트</label>
        <select
          className="select"
          value={kitId}
          onChange={(e) => setKitId(e.target.value)}
        >
          <option value="">키트 선택</option>
          {kits.map((k) => (
            <option key={k.id} value={k.id}>{k.name} v{k.version}{k.is_locked ? " (잠김)" : ""}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>레퍼런스 이미지 ({selected.length}장 선택)</label>
        <div className="row" style={{ flexWrap: "wrap" }}>
          {assets.map((a) => {
            const on = selected.includes(a.id);
            return (
              <label
                key={a.id}
                style={{ cursor: "pointer", border: on ? "2px solid #2563eb" : "1px solid #ddd", borderRadius: 4, padding: 2 }}
              >
                <input type="checkbox" checked={on} onChange={() => toggle(a.id)} style={{ display: "none" }} />
                {a.image_path && <img src={a.image_path} alt={a.name} style={{ width: 56, height: 56, objectFit: "cover", display: "block" }} />}
                <span className="small muted">{a.name.slice(0, 12)}</span>
              </label>
            );
          })}
          {assets.length === 0 && <span className="small muted">이미지 에셋이 없습니다.</span>}
        </div>
      </div>
      <div className="row">
        <button
          className="btn btn-primary"
          disabled={busy || !kitId || selected.length === 0}
          onClick={() => void run()}
        >
          {busy ? "추출 중…" : "이미지에서 추출"}
        </button>
        {cost && <span className="small muted">예상 ~${cost}</span>}
      </div>
      {warning && <p className="small" style={{ color: "#b45309" }}>⚠ {warning}</p>}
      {error && <p className="small" style={{ color: "#dc2626" }}>{error}</p>}
    </section>
  );
}

interface StyleKit {
  id: string;
  name: string;
  version: number;
  is_locked: boolean;
  color?: string | null;
  lighting?: string | null;
  texture?: string | null;
  composition?: string | null;
  lens?: string | null;
}

export default function SettingsPage() {
  const params = useParams();
  const projectId = params.id as string;
  const toast = useToastStore();

  const [project, setProject] = useState<Project | null>(null);
  const [title, setTitle] = useState("");
  const [budget, setBudget] = useState("");
  const [aspect, setAspect] = useState("16:9");
  const [categoryPacks, setCategoryPacks] = useState<CategoryPack[]>([]);
  const [categoryPack, setCategoryPack] = useState("");
  const [kits, setKits] = useState<StyleKit[]>([]);
  const [newKitName, setNewKitName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, packList, kitList] = await Promise.all([
        apiGet<Project>(`/api/v1/projects/${projectId}`),
        apiGet<CategoryPack[]>(`/api/v1/category-packs`),
        apiGet<StyleKit[]>(`/api/v1/projects/${projectId}/style-kits`),
      ]);
      setProject(p);
      setTitle(p.title);
      setBudget(p.monthly_budget_usd != null ? String(p.monthly_budget_usd) : "");
      setAspect(p.aspect_ratio);
      setCategoryPack(p.category_pack_code ?? "");
      setCategoryPacks(packList);
      setKits(kitList);
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    }
  }, [projectId, toast]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const saveProject = async () => {
    setBusy(true);
    try {
      await apiPatch(`/api/v1/projects/${projectId}`, {
        title: title.trim(),
        monthly_budget_usd: budget.trim() ? Number(budget) : null,
        aspect_ratio: aspect,
        category_pack_code: categoryPack || null,
      });
      toast.push("프로젝트 설정을 저장했습니다.");
      await load();
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const createKit = async () => {
    if (!newKitName.trim()) return;
    setBusy(true);
    try {
      await apiPost(`/api/v1/projects/${projectId}/style-kits`, {
        name: newKitName.trim(),
      });
      toast.push("스타일 키트를 만들었습니다.");
      setNewKitName("");
      await load();
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const duplicateKit = async (id: string) => {
    setBusy(true);
    try {
      await apiPost(`/api/v1/style-kits/${id}/duplicate`, {});
      toast.push("키트를 복제해 새 버전을 만들었습니다.");
      await load();
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const lockKit = async (id: string) => {
    setBusy(true);
    try {
      await apiPost(`/api/v1/style-kits/${id}/lock`, {});
      toast.push("키트를 잠갔습니다. 이제 직접 수정할 수 없습니다.");
      await load();
    } catch (e) {
      toast.push(errMsg(e), { kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  if (!project) return <div className="page">불러오는 중…</div>;

  return (
    <div className="page" style={{ maxWidth: 720 }}>
      <div className="page-header">
        <h1>프로젝트 설정</h1>
      </div>

      <section className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">기본 정보</div>
        <div className="field">
          <label>제목</label>
          <input
            className="input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>
        <div className="field">
          <label>월 예산 (USD)</label>
          <input
            className="input"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            placeholder="미설정"
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>화면비 (생성 후 변경 시 기존 샷과 불일치 가능)</label>
          <select
            className="select"
            value={aspect}
            onChange={(e) => setAspect(e.target.value)}
          >
            {["16:9", "9:16", "1:1", "2.39:1", "4:5"].map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>카테고리 팩</label>
          <select
            className="select"
            value={categoryPack}
            onChange={(e) => setCategoryPack(e.target.value)}
          >
            <option value="">없음</option>
            {categoryPacks.map((c) => (
              <option key={c.code} value={c.code}>{c.name}</option>
            ))}
          </select>
        </div>
        <button
          className="btn btn-primary"
          disabled={busy}
          onClick={saveProject}
        >
          저장
        </button>
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">스타일 키트</div>
        <p className="small muted" style={{ marginBottom: 12 }}>
          잠긴 키트는 직접 수정할 수 없습니다. 다른 룩이 필요하면 복제해 새 버전을 만드세요.
        </p>
        <div className="row" style={{ marginBottom: 12 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            value={newKitName}
            onChange={(e) => setNewKitName(e.target.value)}
            placeholder="새 키트 이름"
          />
          <button
            className="btn"
            disabled={busy || !newKitName.trim()}
            onClick={createKit}
          >
            만들기
          </button>
        </div>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {kits.map((k) => (
            <li key={k.id} className="card" style={{ marginBottom: 8, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div>
                <div>
                  {k.name} <span className="small muted">v{k.version}</span>
                  {k.is_locked && (
                    <span className="badge gray" style={{ marginLeft: 8 }}>잠김</span>
                  )}
                </div>
                <div className="small muted">
                  {[k.color, k.lighting, k.lens].filter(Boolean).join(" · ")}
                </div>
              </div>
              <div className="row">
                <button
                  className="btn btn-ghost"
                  disabled={busy}
                  onClick={() => duplicateKit(k.id)}
                >
                  복제
                </button>
                {!k.is_locked && (
                  <button
                    className="btn btn-ghost"
                    disabled={busy}
                    onClick={() => lockKit(k.id)}
                  >
                    잠금
                  </button>
                )}
              </div>
            </li>
          ))}
          {kits.length === 0 && (
            <li className="small muted">아직 스타일 키트가 없습니다.</li>
          )}
        </ul>
      </section>

      <StyleExtractCard projectId={projectId} kits={kits} />
    </div>
  );
}
