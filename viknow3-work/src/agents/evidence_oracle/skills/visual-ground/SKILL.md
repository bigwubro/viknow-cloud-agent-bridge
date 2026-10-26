---
name: visual-ground
description: >
  Use when the user asks WHERE something is in an image, document page, or selected video frame
  ("哪里有X", "X在哪里", "找到X", "图中X的位置"), or HOW MANY physical
  targets are simultaneously visible in one image/frame ("图中有几个X",
  "count the visible X"). Do not use it for OCR/text occurrences, repeated
  actions, durations, or categories accumulated across a video. Call visual_ground
  with query as a COMPLETE NATURAL-LANGUAGE QUESTION that QUALIFIES the target
  (e.g. "图中切好的完整黄色柠檬片有几片？"), never a bare noun like "柠檬片".
  For video counts, pick the frame AFTER the action finishes, then count with
  visual_ground; its detection count is authoritative.
allowed-tools: visual_ground video_inspect_2_0
version: 0.9.0
metadata:
  viknow.kind: workflow
---

# visual_ground 视觉定位与计数

## Overview

`visual_ground` grounds a target in an image, one document page, or one video frame: it returns bounding
boxes (or points) plus a count. It is a **tool**, not a chat model — it does
not answer questions. You (the VLM) must:

1. Turn the user's request into a focused `query` that identifies the target
   and the desired count or location; do not send a bare noun or the whole
   conversation.
2. Phrase the final answer from the structured result (`target`, `count`,
   `detections` with pixel coordinates).

## 输入转换规则（关键）

`query` 应是简短、聚焦的自然语言请求，描述要找或要数的具体目标。保留目标
限定和任务意图；不要只传裸名词，也不要传入无关的整段对话。

| 用户问题 | query（传这个） |
|---|---|
| "哪里有红色螺丝刀" / "红色螺丝刀在哪里" | `图中哪里有红色螺丝刀？` |
| "找到所有车辆" / "图中有几辆车" | `图中有几辆车？` |
| "告诉我按钮的数量" / "有几个按钮" | `画面中一共有几个按钮？` |
| "图片中哪里有问号" / "问号在哪" | `图中哪里有问号？` |
| "where is the person" | `Where is the person in the picture?` |
| "how many people" / "count the cars" | `How many people are in the picture?` |

Rules:
- `query` keeps the target AND the requested count or location. Do not strip it
  down to a bare noun or pass unrelated context from the conversation.
- **限定目标物，避免同类干扰物混入计数。** 当画面中同时存在多类相似目标
  （如黄色柠檬片 vs 青柠片 vs 切半的柠檬 vs 其他圆形物体），query 必须把
  目标限定到用户真正想问的那一类，例如：
  | 用户问题 | 错误的宽泛 query | 正确的限定 query |
  |---|---|---|
  | 切出了几片柠檬 | `图中有几片柠檬？`（把青柠也数进去） | `图中切好的完整黄色柠檬片有几片？` |
  | 有几个完整的鸡蛋 | `图中有几个鸡蛋？`（把碎蛋也数进去） | `画面中完整的未破损鸡蛋有几个？` |
  判断依据：从 `video_inspect_2_0` 的描述或 ASR 字幕（如"两个柠檬切片"）确认
  目标种类与时间窗口，把颜色、完整度、种类等限定词放进 query。
- **结合 ASR/字幕确定目标窗口。** 计数前先看 ASR 转写里对目标的描述
  （如"两个柠檬切片""再切一点青柠"），据此决定 query 限定的种类
  （黄柠檬 vs 青柠）和选帧时间（该动作的结束时刻）。
- Keep one requested target per call. For multiple categories, call separately
  unless the user explicitly asks for a combined count.

## 视频选帧

`visual_ground` 验证一张图片或视频中的一帧。先确定用户所问的时间状态，再
选择能代表该状态、目标可区分且完整可见的帧。若用户问动作完成后的结果，优先
选择完成后的稳定画面；若问某个时刻或过程，则选择那个时刻，而不是默认最终帧。
无法选出这样的帧时，不要从字幕、动作趋势或另一帧推断数字。

多个帧只能帮助选择或排除不合适的帧；不要平均、取最大值或把它们合成视频总数。
最终答案只报告一个明确选定帧的检测结果。

## Tool Boundaries

Use `visual_ground` only for spatial grounding / counting questions on
images, selected document pages, or selected video evidence. Do NOT use it for:
- General knowledge questions (use `knowledge_search` / `knowledge-qa-v2`).
- OCR/subtitle/text occurrence totals, repeated actions, durations, or unique
  categories accumulated across multiple video moments.
- Questions that require combining counts across multiple document pages.
- Scene description or open-ended VQA (there is no such capability here;
  say the tool cannot describe scenes and offer to locate a specific target).

## 文档证据定位

`visual_ground` 对文档证据只处理一个页面：

- 传入 `library_id`、`asset_id`、`evidence_id` 和 `query`。若检索证据已包含
  图片，工具直接处理该图片。
- 若证据只有 PDF 文件，工具从证据元数据解析来源页，并把该页渲染为图片后定位。
- 需要指定其他页面时，传一基页码 `page_number`。不要同时传 `page_number` 和
  `frame_time`。
- 一次调用只证明一个页面上的可见目标；不要把单页结果扩展成整份文档总数。

## 回答组织

The tool returns a structured result. Build the user-facing answer from it:

```json
{
  "target": "问号",
  "intent": "locate",
  "count": 8,
  "detections": [{"box": [75, 409, 180, 637], "label": "问号"}, ...],
  "image_size": {"width": 1280, "height": 720}
}
```

- **计数问题**：answer with the count and the boxes, e.g.
  "图片中共有 **8 个问号**，位于以下位置（像素坐标，图片 1280×720）：
  (75,409)-(180,637)、(183,408)-(287,637)、…"。
- **定位问题**：answer with the locations; if `count == 0`, say the target
  was not found in the image/video.
- Coordinates are in **original image pixels**; state the image size so the
  user can map them.
- For video, `visual_ground` grounds the single frame you choose via
  `frame_time` (see 视频证据定位 below); report the result for that frame.

## 视频证据定位

`visual_ground` 对视频证据只定位**你选定的一帧**(不再自动抽帧聚合):

- 当你持有视频证据(`evidence_id` 来自 `knowledge_search`/`list_video_segments`)
  且已确定目标在哪一帧:传 `evidence_id` + `frame_time`(秒,目标所在帧的时间点),
  工具抽取该帧并返回定位/计数结果。
- 若你还不清楚视频内容、无法判断目标帧:先用 `video_inspect_2_0` 查看帧,
  再根据看到的画面选定 `frame_time`。**不要凭空猜测时间点。**
- 若你已持有目标帧的图片(用户上传的帧图、`video_inspect_2_0` 返回的帧、
  或其他工具输出的帧):直接按图片方式传入(`workspace_path` 或图片证据),
  无需 `frame_time`。
- 返回的坐标是该帧图片的像素坐标,注意结合返回的 `image_size` 说明给用户。

## Non-Negotiables

- `query` identifies one focused target and the requested count or location;
  do not pass a bare noun, unrelated context, or the whole conversation.
- Do not fabricate boxes or counts; use exactly what the tool returned.
- If the target is ambiguous (multiple similar objects in frame), qualify it
  with evidence-backed distinguishing attributes (color, completeness, type).
- A video result supports only its selected `frame_time`; do not turn it into a
  video-wide total, a final-state claim, or an absence claim about another frame.
