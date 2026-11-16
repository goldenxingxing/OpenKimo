import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createInstance } from "i18next";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { I18nextProvider } from "react-i18next";
import {
  formatMessageOutputTime,
  MessageOutputTime,
  type OutputTimeLabels,
  shouldShowMessageOutputTime,
} from "./message-output-time.ts";
import type { LiveMessage } from "../../hooks/types.ts";

const zh: OutputTimeLabels = {
  justNow: "刚刚",
  minutesAgo: (n) => `${n}分钟前`,
  hoursAgo: (n) => `${n}小时前`,
  daysAgo: (n) => `${n}天前`,
  locale: "zh-CN",
};

function loadChat(lang: string): Record<string, unknown> {
  const url = new URL(`../../i18n/locales/${lang}/chat.json`, import.meta.url);
  return JSON.parse(readFileSync(url, "utf8"));
}

async function i18nFor(lang: "en" | "zh-CN") {
  const instance = createInstance();
  await instance.init({
    lng: lang,
    fallbackLng: "en",
    defaultNS: "chat",
    resources: {
      en: { chat: loadChat("en") },
      "zh-CN": { chat: loadChat("zh-CN") },
    },
    interpolation: { escapeValue: false },
  });
  return instance;
}

function renderIn(i18n: Awaited<ReturnType<typeof i18nFor>>, completedAt: number) {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(MessageOutputTime, { completedAt, now }),
    ),
  );
}

const minute = 60_000;
const hour = 60 * minute;
const day = 24 * hour;
const now = Date.UTC(2026, 6, 23, 9, 30, 0);
const TIME_ELEMENT_PATTERN = /<time/;
const DATE_TIME_ATTRIBUTE_PATTERN = /dateTime=/;
const FIVE_MINUTES_AGO_PATTERN = /5分钟前/;
const EN_FIVE_MINUTES_PATTERN = />5 minutes ago</;
const EN_ONE_HOUR_PATTERN = />1 hour ago</;
const EN_JUST_NOW_PATTERN = />just now</;
const ZH_THREE_DAYS_PATTERN = />3天前</;

test("formats assistant output time boundaries", () => {
  assert.equal(formatMessageOutputTime(now - 30_000, now, zh), "刚刚");
  assert.equal(formatMessageOutputTime(now - 5 * minute, now, zh), "5分钟前");
  assert.equal(formatMessageOutputTime(now - 2 * hour, now, zh), "2小时前");
  assert.equal(formatMessageOutputTime(now - 3 * day, now, zh), "3天前");
  assert.equal(
    formatMessageOutputTime(now - 8 * day, now, zh),
    new Date(now - 8 * day).toLocaleDateString("zh-CN"),
  );
});

test("renders semantic time markup", async () => {
  const markup = renderIn(await i18nFor("zh-CN"), now - 5 * minute);

  assert.match(markup, TIME_ELEMENT_PATTERN);
  assert.match(markup, DATE_TIME_ATTRIBUTE_PATTERN);
  assert.match(markup, FIVE_MINUTES_AGO_PATTERN);
});

test("renders the relative time in the active language", async () => {
  const en = await i18nFor("en");
  assert.match(renderIn(en, now - 5 * minute), EN_FIVE_MINUTES_PATTERN);
  assert.match(renderIn(en, now - 1 * hour), EN_ONE_HOUR_PATTERN);
  assert.match(renderIn(en, now - 10_000), EN_JUST_NOW_PATTERN);
  const zhCN = await i18nFor("zh-CN");
  assert.match(renderIn(zhCN, now - 3 * day), ZH_THREE_DAYS_PATTERN);
});

test("shows output time only for completed assistant text", () => {
  const message = (
    overrides: Partial<LiveMessage> = {},
  ): LiveMessage => ({
    id: "message",
    role: "assistant",
    variant: "text",
    content: "Done",
    completedAt: now,
    isStreaming: false,
    ...overrides,
  });

  assert.equal(shouldShowMessageOutputTime(message()), true);
  assert.equal(
    shouldShowMessageOutputTime(message({ isStreaming: true })),
    false,
  );
  assert.equal(
    shouldShowMessageOutputTime(message({ role: "user" })),
    false,
  );
  assert.equal(
    shouldShowMessageOutputTime(message({ variant: "tool" })),
    false,
  );
  assert.equal(
    shouldShowMessageOutputTime(message({ variant: "thinking" })),
    false,
  );
  assert.equal(
    shouldShowMessageOutputTime(message({ completedAt: undefined })),
    false,
  );
});
