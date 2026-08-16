/**
 * 共用的小组件。
 *
 * 抽出来不是为了复用几行 JSX,是为了让**状态与风险的呈现只有一处定义**——散着写的话,
 * 同一个 `ESCALATED` 会在看板上是红的、在列表里是灰的,而用户会以为那是两种不同的东西。
 */
import type { ReactNode } from "react";

export type Tone = "pri" | "ok" | "warn" | "bad" | "pur" | "mute";

/** 状态到色调。**一份映射**,看板与列表共用。 */
const STATE_TONE: Record<string, Tone> = {
  CREATED: "mute",
  DEVELOPING: "pri",
  UNIT_TESTING: "pri",
  INTEGRATION_TESTING: "pur",
  READY_TO_COMMIT: "pur",
  REVIEWING: "warn",
  MERGING: "pri",
  COMPLETED: "ok",
  // ESCALATED 单独用最刺眼的色:它是**最需要人看的状态**,在界面上让它安静等于把系统
  // 在最需要注意力的时候藏起来。
  ESCALATED: "bad",
  CANCELLED: "mute",
};

export function stateTone(state: string): Tone {
  return STATE_TONE[state] ?? "mute";
}

export function Tag({ tone = "mute", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`t ${tone}`}>{children}</span>;
}

export function Card({ title, extra, children }: { title?: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <div className="card">
      {title && (
        <h3>
          {title}
          {extra && <span className="more">{extra}</span>}
        </h3>
      )}
      {children}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  // 空状态给一句话而不是白屏。白屏没法区分"还没有"与"挂了"。
  return <div className="empty">{children}</div>;
}

export function Note({ tone, children }: { tone?: "warn"; children: ReactNode }) {
  return <div className={tone === "warn" ? "note warn" : "note"}>{children}</div>;
}
