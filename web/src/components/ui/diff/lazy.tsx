import { lazy } from "react";
import type { ComponentType } from "react";
import type { DiffProps } from "./index";
import type { Hunk as HunkType, SkipBlock } from "./utils";

type DiffModule = typeof import("./index");

let diffModulePromise: Promise<DiffModule> | null = null;

const loadDiffModule = async (): Promise<DiffModule> => {
  if (!diffModulePromise) {
    diffModulePromise = import("./index").catch((error: unknown) => {
      diffModulePromise = null;
      throw error;
    });
  }
  return diffModulePromise;
};

export const LazyDiff = lazy(
  async (): Promise<{ default: ComponentType<DiffProps> }> => {
    const module = await loadDiffModule();
    return { default: module.Diff };
  },
);

export const LazyHunk = lazy(
  async (): Promise<{
    default: ComponentType<{ hunk: HunkType | SkipBlock }>;
  }> => {
    const module = await loadDiffModule();
    return { default: module.Hunk };
  },
);
