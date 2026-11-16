import { cn } from "@/lib/utils";
import type { ComponentProps, ReactNode } from "react";
import { isValidElement, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { FolderOpen } from "lucide-react";
import type { MouseEvent } from "react";
import type { StreamdownProps } from "streamdown";
import { defaultRehypePlugins, defaultRemarkPlugins } from "streamdown";
import { CodeBlock } from "./code-block";
import {
  decodeOpenPathHref,
  isOpenPathHref,
  isLikelyStandalonePath,
  resolveLocalLinkPath,
  resolveMentionedPath,
  usePathWorkDir,
} from "@/features/chat/message-paths";
import {
  openViaBackend,
  resolveViaBackend,
  type BackendApp,
} from "@/features/chat/open-in-shared";

/** Shared click flow: resolve the mention to a real file, then open it. */
function useOpenMentionedPath() {
  const { t } = useTranslation("chat");
  const workDir = usePathWorkDir();

  return async (
    path: string,
    event: { metaKey?: boolean; ctrlKey?: boolean },
    appOverride?: BackendApp,
  ) => {
    const app = appOverride ?? (event.metaKey || event.ctrlKey ? "finder" : "default");
    try {
      const target = await resolveViaBackend(path, workDir);
      await openViaBackend(app, target);
    } catch (error) {
      toast.error(t("openIn.openFailed"), {
        description:
          error instanceof Error
            ? error.message
            : t("openIn.unexpectedError"),
      });
    }
  };
}

// How long the pointer must rest on a path link before the hover menu
// (open containing folder) appears.
const HOVER_REVEAL_DELAY_MS = 600;

/** Hover-with-delay state: reveal a menu after the pointer rests a moment. */
function useHoverReveal(delayMs: number = HOVER_REVEAL_DELAY_MS) {
  const [open, setOpen] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const start = () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(true), delayMs);
  };
  const stop = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    setOpen(false);
  };

  return { open, start, stop, close: stop };
}

/** Small menu that appears under a path link after a hover delay. */
function PathHoverMenu({
  open,
  onClose,
  onOpenFolder,
}: {
  open: boolean;
  onClose: () => void;
  onOpenFolder: () => void;
}) {
  const { t } = useTranslation("chat");
  if (!open) {
    return null;
  }
  return (
    <span
      role="menu"
      className="absolute left-0 top-full z-20 pt-1"
      // Keep focus on the link (no blur) and stop text selection while the
      // pointer is inside the menu, so a stray mouse-down does not dismiss it.
      onMouseDown={(event) => event.preventDefault()}
    >
      <button
        type="button"
        role="menuitem"
        className="flex items-center gap-1 whitespace-nowrap rounded-md border bg-background px-2 py-1 text-xs shadow-md hover:bg-accent hover:text-accent-foreground"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onOpenFolder();
          onClose();
        }}
      >
        <FolderOpen className="h-3.5 w-3.5" />
        {t("pathMentions.openContainingFolder")}
      </button>
    </span>
  );
}

// Selectively enable rehype plugins while maintaining security.
// - 'raw': renders raw HTML embedded in markdown (XSS risk - DISABLED)
// - 'harden': sanitizes HTML (strips tags - DISABLED)
// - 'katex': math rendering (SAFE - only processes math delimiters)
export const safeRehypePlugins: StreamdownProps["rehypePlugins"] = [
  defaultRehypePlugins.katex,
];

// Override remark-math to enable single-dollar inline math ($...$).
// Streamdown defaults to singleDollarTextMath: false, which only allows
// block math ($$...$$). We enable it so both $...$ and $$...$$ work.
const mathPlugin = defaultRemarkPlugins.math;
const remarkMathWithInline = (
  Array.isArray(mathPlugin)
    ? [mathPlugin[0], { ...mathPlugin[1], singleDollarTextMath: true }]
    : [mathPlugin, { singleDollarTextMath: true }]
) as typeof mathPlugin;

export const safeRemarkPlugins: StreamdownProps["remarkPlugins"] = [
  defaultRemarkPlugins.gfm,
  remarkMathWithInline,
  defaultRemarkPlugins.cjkFriendly,
  defaultRemarkPlugins.cjkFriendlyGfmStrikethrough,
];

/**
 * Escape HTML-like tags outside of code blocks to prevent XSS and preserve
 * markdown structure. HTML tags like <script>, <img>, etc. break markdown
 * parsing (especially numbered lists) because remark treats them as HTML nodes.
 *
 * This function:
 * 1. Preserves content inside code blocks (```...``` and `...`)
 * 2. Preserves content inside math delimiters ($...$ and $$...$$)
 * 3. Escapes both < and > in HTML-like tags elsewhere
 */
export const escapeHtmlOutsideCodeBlocks = (text: string): string => {
  // Match regions that should NOT be escaped:
  // 1. Fenced code blocks: ``` at line start, optional language, content, then \n```
  // 2. Inline code: `..` (single backticks, no newlines inside)
  // 3. Display math: $$...$$ (can span multiple lines)
  // 4. Inline math: $...$ (no newlines, non-empty, no leading/trailing space)
  const codeBlockRegex =
    /(^|\n)(```|~~~)[a-z]*\n[\s\S]*?\n\2|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$(?!\s)[^$\n]+(?<!\s)\$/g;
  const codeBlocks: { start: number; end: number }[] = [];

  // Find all valid code blocks
  let match;
  while ((match = codeBlockRegex.exec(text)) !== null) {
    // Adjust start position if match includes leading newline
    const startsWithNewline = match[0].startsWith("\n");
    const start = startsWithNewline ? match.index + 1 : match.index;
    codeBlocks.push({ start, end: match.index + match[0].length });
  }

  // Escape content outside code blocks to prevent markdown from misinterpreting it.
  const escapeForMarkdown = (str: string): string => {
    // 1. Escape HTML-like tags with fullwidth Unicode equivalents (＜ and ＞)
    //    which look nearly identical but won't be parsed as HTML tags.
    let result = str.replace(/<(?=[a-zA-Z/!?])/g, "＜");
    // Exclude line-start > (blockquotes) and arrows (-> and =>)
    result = result.replace(/(?<!^)(?<![-=])>/gm, "＞");

    // 2. Prevent indented code blocks: insert zero-width space after newline
    //    before 4+ spaces. This breaks the CommonMark indented code block pattern.
    result = result.replace(/\n([ ]{4,})/g, "\n\u200B$1");

    return result;
  };

  // Process text, escaping content outside code blocks
  const result: string[] = [];
  let lastEnd = 0;

  for (const block of codeBlocks) {
    // Process text before this code block
    const before = text.slice(lastEnd, block.start);
    result.push(escapeForMarkdown(before));
    // Keep code block unchanged
    result.push(text.slice(block.start, block.end));
    lastEnd = block.end;
  }

  // Process remaining text after last code block
  const after = text.slice(lastEnd);
  result.push(escapeForMarkdown(after));

  return result.join("");
};

// Prevent Streamdown margins from collapsing, which can break Virtuoso height measurement.
export const streamdownRootClass = [
  "flow-root",
  "[&_p]:my-0",
  "[&_ul]:my-0",
  "[&_ol]:my-0",
  "[&_li]:my-0",
  "[&_blockquote]:my-0",
  "[&_pre]:my-0",
  "[&_*+p]:mt-3",
  "[&_*+ul]:mt-3",
  "[&_*+ol]:mt-3",
  "[&_*+blockquote]:mt-3",
  "[&_*+pre]:mt-3",
  "[&_pre+*]:mt-3",
  "[&_li+li]:mt-1",
  "[&_hr]:my-4",
  "[&_h1]:mt-6 [&_h1]:mb-2",
  "[&_h2]:mt-5 [&_h2]:mb-2",
  "[&_h3]:mt-4 [&_h3]:mb-1.5",
  "[&_h4]:mt-3 [&_h4]:mb-1",
  "[&_h5]:mt-3 [&_h5]:mb-1",
  "[&_h6]:mt-3 [&_h6]:mb-1",
].join(" ");

const LANGUAGE_CLASS_RE = /language-([^\s]+)/;

const getCodeLanguage = (className?: string): string | undefined => {
  const match = className?.match(LANGUAGE_CLASS_RE);
  return match?.[1];
};

const getCodeText = (children: ReactNode): string => {
  if (typeof children === "string") {
    return children;
  }
  if (Array.isArray(children)) {
    return children.map(getCodeText).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(children)) {
    return getCodeText(children.props.children);
  }
  return "";
};

type StreamdownCodeProps = ComponentProps<"code"> & {
  node?: {
    position?: {
      start?: { line?: number };
      end?: { line?: number };
    };
  };
};

/**
 * A path mention rendered as a clickable link inside the message text.
 * Plain click opens it (default app; a directory opens in Finder);
 * Cmd/Ctrl+click reveals it in Finder instead.
 */
function OpenPathAnchor({
  href,
  children,
  ...props
}: ComponentProps<"a">) {
  const { t } = useTranslation("chat");
  const openMentioned = useOpenMentionedPath();
  const workDir = usePathWorkDir();
  const hover = useHoverReveal();
  const path = isOpenPathHref(href)
    ? decodeOpenPathHref(href)
    : resolveLocalLinkPath(href, workDir);
  if (path === null) {
    // Regular web link: reproduce Streamdown's own anchor styling,
    // which our override would otherwise swallow.
    return (
      <a
        className={cn("wrap-anywhere font-medium text-primary underline", props.className)}
        data-streamdown="link"
        href={href}
        rel="noreferrer"
        target="_blank"
        {...props}
      >
        {children}
      </a>
    );
  }

  const open = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void openMentioned(path, event);
  };

  return (
    <span
      className="relative inline-block"
      onMouseEnter={hover.start}
      onMouseLeave={hover.stop}
    >
      <a
        href="#"
        title={t("pathMentions.clickHint", { path })}
        data-open-path="true"
        aria-haspopup="menu"
        aria-expanded={hover.open}
        onClick={(event) => {
          hover.close();
          open(event);
        }}
        {...props}
        className={cn(
          "cursor-pointer underline decoration-dotted underline-offset-2",
          "hover:text-primary",
          props.className,
        )}
      >
        {children}
      </a>
      <PathHoverMenu
        open={hover.open}
        onClose={hover.close}
        onOpenFolder={() => void openMentioned(path, {}, "finder")}
      />
    </span>
  );
}

/** Inline code whose whole content is one path mention — make it clickable. */
function InlinePathCode({
  children,
  className,
  ...props
}: ComponentProps<"code">) {
  const { t } = useTranslation("chat");
  const openMentioned = useOpenMentionedPath();
  const workDir = usePathWorkDir();
  const hover = useHoverReveal();
  const text = getCodeText(children);
  if (!isLikelyStandalonePath(text, workDir)) {
    return null;
  }
  const path = resolveMentionedPath(text, workDir);

  const open = (event: MouseEvent<HTMLSpanElement>) => {
    event.preventDefault();
    void openMentioned(path, event);
  };

  return (
    <code
      className={cn(
        "rounded bg-secondary px-1 py-0.5 font-mono text-xs",
        className,
      )}
      data-streamdown="inline-code"
      {...props}
    >
      <span
        className="relative inline-block"
        onMouseEnter={hover.start}
        onMouseLeave={hover.stop}
      >
        <span
          className="cursor-pointer underline decoration-dotted underline-offset-2 hover:text-primary"
          title={t("pathMentions.clickHint", { path })}
          aria-haspopup="menu"
          aria-expanded={hover.open}
          onClick={(event) => {
            hover.close();
            open(event);
          }}
        >
          {children}
        </span>
        <PathHoverMenu
          open={hover.open}
          onClose={hover.close}
          onOpenFolder={() => void openMentioned(path, {}, "finder")}
        />
      </span>
    </code>
  );
}

const StreamdownCode = ({
  className,
  children,
  node,
  ...props
}: StreamdownCodeProps) => {
  const workDir = usePathWorkDir();
  const isInline = node?.position?.start?.line === node?.position?.end?.line;

  if (isInline) {
    // A backticked path is the most common way an agent cites a file; offer
    // the open action right where the reader sees it.
    if (isLikelyStandalonePath(getCodeText(children), workDir)) {
      return (
        <InlinePathCode className={className} {...props}>
          {children}
        </InlinePathCode>
      );
    }
    return (
      <code
        className={cn(
          "rounded bg-secondary px-1 py-0.5 font-mono text-xs",
          className,
        )}
        data-streamdown="inline-code"
        {...props}
      >
        {children}
      </code>
    );
  }

  return (
    <CodeBlock
      className={cn("my-2", className)}
      code={getCodeText(children)}
      language={getCodeLanguage(className)}
      {...props}
    />
  );
};

const StreamdownPre = ({ children }: ComponentProps<"pre">) => children;

export const streamdownComponents: StreamdownProps["components"] = {
  a: OpenPathAnchor,
  code: StreamdownCode,
  pre: StreamdownPre,
};
