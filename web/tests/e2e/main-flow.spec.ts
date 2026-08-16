import { expect, test } from "@playwright/test";

test("submit → requirement → run → live stage → refresh → activity reopen", async ({ page }) => {
  await page.goto("/#/submit");
  await page.getByPlaceholder(/谁、在什么情况下、要得到什么/).fill("真实浏览器主流程回归");
  await page.getByRole("button", { name: "提交需求", exact: true }).click();

  await expect(page).toHaveURL(/#\/requirements\/req-/);
  const taskLink = page.getByRole("link", { name: /ag-/ }).first();
  const taskId = await taskLink.textContent();
  expect(taskId).toBeTruthy();
  await taskLink.click();
  await expect(page).toHaveURL(new RegExp(`#/tasks/${taskId}$`));

  await page.getByRole("button", { name: "开始执行", exact: true }).click();
  // 开发回放故意不提供录制：流程会很快进入失败事实。用持久化事件证明曾真实推进到
  // DEVELOPING，比捕获一个可能只显示几毫秒的瞬时标签更稳定。
  await expect(page.getByText("状态从 待启动 变为 开发中", { exact: true })).toBeVisible();
  await expect(page.getByText("执行已中断", { exact: true }).first()).toBeVisible();

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`#/tasks/${taskId}$`));
  await expect(page.getByRole("heading", { name: "真实浏览器主流程回归" })).toBeVisible();

  await page.getByRole("button", { name: /观测中心/ }).click();
  await page.getByRole("link", { name: taskId! }).first().click();
  await expect(page).toHaveURL(new RegExp(`#/tasks/${taskId}$`));
});
