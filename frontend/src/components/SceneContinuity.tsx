import { useState } from "react";
import { Images, RefreshCw } from "lucide-react";
import type { ProductionScene, ScenePosition } from "../api";
import { useT } from "../i18n";
import { useApp } from "./ui";

const positions = ["first", "middle", "last"] as const;
const positionLabels = { first: "sceneFirst", middle: "sceneMiddle", last: "sceneLast" } as const;
const verdictLabels = { pass: "scenePass", defect: "sceneDefect", unclear: "sceneUnclear" } as const;

function sampleUrl(slug: string, key: string, clip: string, position: ScenePosition) {
  return `/generation/api/productions/${slug}/scene/${key}/sample/${clip}/${position}.jpg`;
}

export function SceneContinuity({ slug, scene, disabled, locked, onRedo }: {
  slug: string;
  scene: ProductionScene;
  disabled: boolean;
  locked: string[];
  onRedo: (key: string) => void;
}) {
  const { t, lang } = useT();
  const app = useApp();
  const [open, setOpen] = useState<string[]>([]);
  const seconds = new Intl.NumberFormat(lang, { maximumFractionDigits: 2 });
  return (
    <section className="scene-continuity" aria-label={t("sceneContinuity")}>
      <div className="row wrap">
        <h3 className="grow">{t("sceneContinuity")}</h3>
        <button className="btn sm" onClick={() => app.openAsset(scene.character_sheet_asset_id)}>
          <Images size={14} /> {t("sceneSheet")}
        </button>
      </div>
      {scene.shots.map((shot) => {
        const { review, clip_asset_id: clip } = shot;
        const status = review ? t(verdictLabels[review.status]) : t(clip ? "sceneUnreviewed" : "sceneNoClip");
        const openEvidence = open.includes(shot.key);
        const anchors = shot.last_frame_asset_id ? [shot.first_frame_asset_id, shot.last_frame_asset_id] : [shot.first_frame_asset_id];
        return (
          <div className="scene-shot" key={shot.key}>
            <div className="row wrap">
              <strong className="grow">{t("sbShotN", { n: shot.key })} · {shot.framing} · {seconds.format(shot.duration_s)} s</strong>
              <span className={`pill ${review?.status === "defect" || review?.status === "unclear" ? "warn" : ""}`}>{status}</span>
              {clip && <button className="btn sm" disabled={disabled || locked.includes(shot.key)} onClick={() => onRedo(shot.key)}>
                <RefreshCw size={14} /> {t("sceneRedo", { n: shot.key })}
              </button>}
            </div>
            <p>{shot.action}</p>
            <div className="row wrap">
              <button className="btn sm ghost" onClick={() => app.openAsset(anchors[0], anchors)}>
                {t(shot.last_frame_asset_id ? "sceneAnchors" : "sceneFirst")}
              </button>
              {shot.control_asset_id && <button className="btn sm ghost" onClick={() => app.openAsset(shot.control_asset_id!)}>
                {t("sceneStructure")}
              </button>}
            </div>
            {clip && <details className="scene-evidence" onToggle={(event) => {
              const expanded = event.currentTarget.open;
              setOpen((keys) => expanded ? [...new Set([...keys, shot.key])] : keys.filter((key) => key !== shot.key));
            }}>
              <summary>{t("sceneEvidence")}</summary>
              {openEvidence && <div className="scene-samples">
                {positions.map((position) => (
                  <figure key={position}>
                    <img src={sampleUrl(slug, shot.key, clip, position)} alt={t(positionLabels[position])} loading="lazy" />
                    <figcaption className="small">{t(positionLabels[position])}</figcaption>
                    {review?.samples.find((sample) => sample.position === position)?.checks.length && <ul className="small">
                      {review.samples.find((sample) => sample.position === position)?.checks.map((check, i) => (
                        <li key={scene.preserve_details[i]}>
                          <strong>{scene.preserve_details[i]}</strong> · {t(verdictLabels[check.verdict])}. {check.note}
                        </li>
                      ))}
                    </ul>}
                  </figure>
                ))}
              </div>}
            </details>}
          </div>
        );
      })}
    </section>
  );
}
