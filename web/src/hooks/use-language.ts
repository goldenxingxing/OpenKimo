import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import { normalizeLanguage, type SupportedLang } from "@/i18n";

type UseLanguageResult = {
  language: SupportedLang;
  setLanguage: (next: SupportedLang) => void;
  toggleLanguage: () => void;
};

export function useLanguage(): UseLanguageResult {
  // Subscribe to react-i18next so consumers re-render on language change.
  const { i18n } = useTranslation();
  const language = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language);

  const setLanguage = useCallback(
    (next: SupportedLang) => {
      // i18next's detector already caches the choice (caches: ["localStorage"]);
      // writing here only duplicated it, and a blocked storage could abort the
      // language change before it started.
      i18n.changeLanguage(next).catch(() => undefined);
    },
    [i18n],
  );

  const toggleLanguage = useCallback(() => {
    setLanguage(language === "zh-CN" ? "en" : "zh-CN");
  }, [language, setLanguage]);

  return { language, setLanguage, toggleLanguage };
}
