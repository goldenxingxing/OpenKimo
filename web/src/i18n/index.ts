import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import enBackend from "./locales/en/backend.json";
import enChat from "./locales/en/chat.json";
import enCommon from "./locales/en/common.json";
import enConfig from "./locales/en/config.json";
import enErrors from "./locales/en/errors.json";
import enSessions from "./locales/en/sessions.json";
import enToasts from "./locales/en/toasts.json";
import enTokens from "./locales/en/tokens.json";
import enAdmin from "./locales/en/admin.json";
import enSkills from "./locales/en/skills.json";
import enWiki from "./locales/en/wiki.json";
import enMemory from "./locales/en/memory.json";
import enAuth from "./locales/en/auth.json";
import enTools from "./locales/en/tools.json";
import zhBackend from "./locales/zh-CN/backend.json";
import zhChat from "./locales/zh-CN/chat.json";
import zhCommon from "./locales/zh-CN/common.json";
import zhConfig from "./locales/zh-CN/config.json";
import zhErrors from "./locales/zh-CN/errors.json";
import zhSessions from "./locales/zh-CN/sessions.json";
import zhToasts from "./locales/zh-CN/toasts.json";
import zhTokens from "./locales/zh-CN/tokens.json";
import zhAdmin from "./locales/zh-CN/admin.json";
import zhSkills from "./locales/zh-CN/skills.json";
import zhWiki from "./locales/zh-CN/wiki.json";
import zhMemory from "./locales/zh-CN/memory.json";
import zhAuth from "./locales/zh-CN/auth.json";
import zhTools from "./locales/zh-CN/tools.json";

export const SUPPORTED_LANGS = ["en", "zh-CN"] as const;
export type SupportedLang = (typeof SUPPORTED_LANGS)[number];

export const LANGUAGE_STORAGE_KEY = "kimo-language";

export const I18N_NAMESPACES = [
  "common",
  "sessions",
  "chat",
  "config",
  "toasts",
  "backend",
  "errors",
  "tokens",
  "admin",
  "skills",
  "wiki",
  "memory",
  "auth",
  "tools"
] as const;

const resources = {
  en: {
    common: enCommon,
    sessions: enSessions,
    chat: enChat,
    config: enConfig,
    toasts: enToasts,
    backend: enBackend,
    errors: enErrors,
    tokens: enTokens,
    admin: enAdmin,
    skills: enSkills,
    wiki: enWiki,
    memory: enMemory,
    auth: enAuth,
    tools: enTools
  },
  "zh-CN": {
    common: zhCommon,
    sessions: zhSessions,
    chat: zhChat,
    config: zhConfig,
    toasts: zhToasts,
    backend: zhBackend,
    errors: zhErrors,
    tokens: zhTokens,
    admin: zhAdmin,
    skills: zhSkills,
    wiki: zhWiki,
    memory: zhMemory,
    auth: zhAuth,
    tools: zhTools
  },
} as const;

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: "en",
    supportedLngs: [...SUPPORTED_LANGS],
    load: "currentOnly",
    defaultNS: "common",
    ns: [...I18N_NAMESPACES],
    interpolation: {
      escapeValue: false,
    },
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: LANGUAGE_STORAGE_KEY,
      caches: ["localStorage"],
      convertDetectedLanguage: (lng) => normalizeLanguage(lng),
    },
    returnNull: false,
    react: {
      useSuspense: false,
    },
  });

export function normalizeLanguage(raw: string | undefined | null): SupportedLang {
  if (!raw) return "en";
  if (raw === "zh-CN" || raw.toLowerCase().startsWith("zh")) return "zh-CN";
  return "en";
}

export default i18n;
