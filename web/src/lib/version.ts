declare const __KIMO_CLI_VERSION__: string | undefined;

export const kimoCliVersion =
  typeof __KIMO_CLI_VERSION__ !== "undefined" && __KIMO_CLI_VERSION__
    ? __KIMO_CLI_VERSION__
    : "dev";
