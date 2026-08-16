/**
 * 顶栏:工作区、搜索、身份。
 *
 * **样式一直在 `styles.css` 里,却没有任何组件用它们**(`.top` / `.ws` / `.search` / `.av`)
 * ——这是"顶栏被漏掉了"最硬的证据,不是"决定不做"。
 *
 * ## 身份搬到这里,侧边栏底部那个输入框撤掉
 *
 * 两处都放身份的话,用户会问哪个才算数。**搬家不改语义**:它仍然只是一个前端声明,
 * 服务端照样二次校验(PRD 18 之前都是如此)——**顶栏这个头像不是登录态**,别让它看起来像。
 *
 * ## 搜索框只放形,不接功能
 *
 * ⌘K 已经在样式里占了位,但真正的全局搜索是另一件事。**放一个不工作的搜索框比不放更糟**,
 * 所以点它给一句"还没做",而不是假装能搜。
 */
import { useState } from "react";
import type { WorkspaceEntry } from "../api/client";

//: 切换器里"新建"那一项的哨兵值。项目名的形状不允许 `+`,不会撞真名。
const NEW_PROJECT = "+new";

export function TopBar({
  actor,
  onActor,
  projects,
  current,
  onSwitch,
  onNewProject,
}: {
  actor: string;
  onActor: (value: string) => void;
  /** 项目清单由 App 拉取并持有——它还要用同一份数据判断"零项目引导页"。 */
  projects: WorkspaceEntry[];
  current: string;
  onSwitch: (name: string) => void;
  onNewProject: () => void;
}) {
  const [note, setNote] = useState("");

  return (
    <div className="top">
      <div className="ws">
        <i>🧬</i>
        {projects.length <= 1 ? (
          // 单项目不摆下拉:没有可切的,摆一个只有一项的选择框是在暗示不存在的能力。
          <span>{current || projects[0]?.name || "(未命名工作区)"}</span>
        ) : (
          <select
            aria-label="切换项目"
            value={current}
            onChange={(event) => {
              if (event.target.value === NEW_PROJECT) onNewProject();
              else onSwitch(event.target.value);
            }}
          >
            {projects.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name}
                {item.initializing ? " · 初始化中" : ""}
              </option>
            ))}
            <option value={NEW_PROJECT}>＋ 新项目</option>
          </select>
        )}
        {projects.length === 1 && (
          <button className="btn" style={{ marginLeft: 8 }} onClick={onNewProject}>
            ＋ 新项目
          </button>
        )}
      </div>

      <div
        className="search"
        role="button"
        onClick={() => setNote("全局搜索还没做——先用各页自己的筛选。")}
      >
        🔍 搜索需求、任务、文档
        <kbd>⌘K</kbd>
      </div>
      {note && <span className="m">{note}</span>}

      <div className="me">
        {/* 身份是前端声明,**服务端二次校验**;这里不是登录态。 */}
        <input
          aria-label="你的身份"
          value={actor}
          onChange={(event) => onActor(event.target.value)}
          placeholder="审批时要填"
          style={{ width: 140 }}
        />
        <span className="av">{actor.slice(0, 1) || "?"}</span>
      </div>
    </div>
  );
}
