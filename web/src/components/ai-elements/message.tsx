"use client";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { ButtonGroup, ButtonGroupText } from "@/components/ui/button-group";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { UIMessage } from "ai";
import type { MessageAttachmentPart, NoPreviewAttachment, VideoNoPreviewAttachment } from "@/hooks/types";
import { useVideoThumbnail } from "@/hooks/useVideoThumbnail";
import { type FileKind, fileExtension, fileKind } from "./file-kind";
import { findFileTokens, makeToken, splitPastedBlocks } from "./prompt-input-paste";
import { useTranslation } from "react-i18next";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  FileArchiveIcon,
  FileAudioIcon,
  FileCodeIcon,
  FileIcon,
  FileImageIcon,
  FileJsonIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  FileVideoIcon,
  GitBranchIcon,
  PresentationIcon,
  XIcon,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes, ReactElement, ReactNode } from "react";
import { createContext, memo, useContext, useEffect, useState } from "react";
import { Streamdown } from "streamdown";
import {
  escapeHtmlOutsideCodeBlocks,
  safeRehypePlugins,
  safeRemarkPlugins,
  streamdownComponents,
  streamdownRootClass,
} from "./streamdown";

export type MessageProps = HTMLAttributes<HTMLDivElement> & {
  from: UIMessage["role"];
};

export const Message = ({ className, from, ...props }: MessageProps) => (
  <div
    className={cn(
      "group flex w-full flex-col gap-1",
      from === "user" ? "is-user" : "is-assistant",
      className,
    )}
    {...props}
  />
);

export type MessageContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageContent = ({
  children,
  className,
  ...props
}: MessageContentProps) => (
  <div
    className={cn(
      "flex w-full flex-col gap-1 overflow-hidden text-sm",
      className,
    )}
    {...props}
  >
    {children}
  </div>
);

/** User message content with bubble styling */
export type UserMessageContentProps = HTMLAttributes<HTMLDivElement>;

export const UserMessageContent = ({
  children,
  className,
  ...props
}: UserMessageContentProps) => {
  return (
    <div
      className={cn(
        "ml-auto w-fit max-w-[85%] rounded-2xl bg-secondary/50 px-4 py-3 text-sm",
        "dark:bg-secondary/30",
        className,
      )}
      {...props}
    >
      <div className="whitespace-pre-wrap break-words">
        {typeof children === "string" ? <WithFoldedPastes text={children} /> : children}
      </div>
    </div>
  );
};

/** A sent message with each pasted block shown as its placeholder, openable in place. */
function WithFoldedPastes({ text }: { text: string }) {
  const segments = splitPastedBlocks(text);
  if (segments.length === 1 && segments[0].kind === "text") {
    return <WithFileTokens text={text} />;
  }
  return segments.map((segment, index) =>
    segment.kind === "text" ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional and fixed
      <WithFileTokens key={index} text={segment.text} />
    ) : (
      // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional and fixed
      <FoldedPaste content={segment.content} index={segment.index} key={index} />
    ),
  );
}

/** A file the person placed in their text, tinted like it was in the composer. */
function WithFileTokens({ text }: { text: string }) {
  const tokens = findFileTokens(text);
  if (tokens.length === 0) return text;
  const parts: ReactNode[] = [];
  let last = 0;
  for (const token of tokens) {
    parts.push(text.slice(last, token.start));
    parts.push(
      <span className="mx-0.5 rounded-sm bg-primary/15 px-0.5" key={token.start}>
        {text.slice(token.start, token.end)}
      </span>,
    );
    last = token.end;
  }
  parts.push(text.slice(last));
  return parts;
}

function FoldedPaste({ index, content }: { index: number; content: string }) {
  const { t } = useTranslation("chat");
  const [open, setOpen] = useState(false);
  const label = makeToken(index, content, {
    noun: t("paste.noun"),
    lines: (count) => t("paste.lines", { count }),
    chars: (count) => t("paste.chars", { count }),
  });
  return (
    <>
      <button
        className="mx-0.5 rounded-sm bg-primary/15 px-0.5 hover:bg-primary/25"
        onClick={() => setOpen((value) => !value)}
        title={open ? t("paste.collapse") : t("paste.view")}
        type="button"
      >
        {label}
      </button>
      {open ? (
        <span className="my-1 block max-h-80 overflow-auto rounded-md border bg-background/60 p-2 font-mono text-xs">
          {content}
        </span>
      ) : null}
    </>
  );
}

export type MessageActionsProps = ComponentProps<"div">;

export const MessageActions = ({
  className,
  children,
  ...props
}: MessageActionsProps) => (
  <div className={cn("flex items-center gap-1 ml-4", className)} {...props}>
    {children}
  </div>
);

export type MessageActionProps = ComponentProps<typeof Button> & {
  tooltip?: string;
  label?: string;
};

export const MessageAction = ({
  tooltip,
  children,
  label,
  variant = "ghost",
  size = "icon-sm",
  className,
  ...props
}: MessageActionProps) => {
  const button = (
    <Button size={size} type="button" variant={variant} className={cn("size-6", className)} {...props}>
      {children}
      <span className="sr-only">{label || tooltip}</span>
    </Button>
  );

  if (tooltip) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{button}</TooltipTrigger>
          <TooltipContent className="px-1.5 py-0.5">
            <p className="text-[12px]">{tooltip}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return button;
};

export type MessageCopyButtonProps = {
  content: string;
  timeout?: number;
};

export const MessageCopyButton = ({
  content,
  timeout = 2000,
}: MessageCopyButtonProps) => {
  const { t } = useTranslation("chat");
  const [isCopied, setIsCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setIsCopied(true);
      setTimeout(() => setIsCopied(false), timeout);
    } catch {
      // Clipboard API not available or write failed
    }
  };

  return (
    <MessageAction tooltip={isCopied ? t("message.copied") : t("message.copy")} onClick={handleCopy}>
      {isCopied ? <CheckIcon className="size-3" /> : <CopyIcon className="size-3" />}
    </MessageAction>
  );
};

export type MessageForkButtonProps = {
  onFork: () => void;
};

export const MessageForkButton = ({ onFork }: MessageForkButtonProps) => {
  const { t } = useTranslation(["chat", "common"]);
  return (
    <AlertDialog>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <AlertDialogTrigger asChild>
              <Button size="icon-sm" type="button" variant="ghost" className="size-6">
                <GitBranchIcon className="size-3" />
                <span className="sr-only">{t("chat:fork.button")}</span>
              </Button>
            </AlertDialogTrigger>
          </TooltipTrigger>
          <TooltipContent className="px-1.5 py-0.5">
            <p className="text-[12px]">{t("chat:fork.tooltip")}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <AlertDialogContent size="sm">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("chat:fork.title")}</AlertDialogTitle>
          <AlertDialogDescription>
            {t("chat:fork.description")}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("common:actions.cancel")}</AlertDialogCancel>
          <AlertDialogAction onClick={onFork}>{t("chat:fork.confirm")}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
};

type MessageBranchContextType = {
  currentBranch: number;
  totalBranches: number;
  goToPrevious: () => void;
  goToNext: () => void;
  branches: ReactElement[];
  setBranches: (branches: ReactElement[]) => void;
};

const MessageBranchContext = createContext<MessageBranchContextType | null>(
  null,
);

const useMessageBranch = () => {
  const context = useContext(MessageBranchContext);

  if (!context) {
    throw new Error(
      "MessageBranch components must be used within MessageBranch",
    );
  }

  return context;
};

export type MessageBranchProps = HTMLAttributes<HTMLDivElement> & {
  defaultBranch?: number;
  onBranchChange?: (branchIndex: number) => void;
};

export const MessageBranch = ({
  defaultBranch = 0,
  onBranchChange,
  className,
  ...props
}: MessageBranchProps) => {
  const [currentBranch, setCurrentBranch] = useState(defaultBranch);
  const [branches, setBranches] = useState<ReactElement[]>([]);

  const handleBranchChange = (newBranch: number) => {
    setCurrentBranch(newBranch);
    onBranchChange?.(newBranch);
  };

  const goToPrevious = () => {
    const newBranch =
      currentBranch > 0 ? currentBranch - 1 : branches.length - 1;
    handleBranchChange(newBranch);
  };

  const goToNext = () => {
    const newBranch =
      currentBranch < branches.length - 1 ? currentBranch + 1 : 0;
    handleBranchChange(newBranch);
  };

  const contextValue: MessageBranchContextType = {
    currentBranch,
    totalBranches: branches.length,
    goToPrevious,
    goToNext,
    branches,
    setBranches,
  };

  return (
    <MessageBranchContext.Provider value={contextValue}>
      <div
        className={cn("grid w-full gap-2 [&>div]:pb-0", className)}
        {...props}
      />
    </MessageBranchContext.Provider>
  );
};

export type MessageBranchContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageBranchContent = ({
  children,
  ...props
}: MessageBranchContentProps) => {
  const { currentBranch, setBranches, branches } = useMessageBranch();
  const childrenArray = Array.isArray(children) ? children : [children];

  // Use useEffect to update branches when they change
  useEffect(() => {
    if (branches.length !== childrenArray.length) {
      setBranches(childrenArray);
    }
  }, [childrenArray, branches, setBranches]);

  return childrenArray.map((branch, index) => (
    <div
      className={cn(
        "grid gap-2 overflow-hidden",
        index === currentBranch ? "block" : "hidden",
      )}
      key={branch.key}
      {...props}
    >
      {branch}
    </div>
  ));
};

export type MessageBranchSelectorProps = HTMLAttributes<HTMLDivElement> & {
  from: UIMessage["role"];
};

export const MessageBranchSelector = ({
  className,
  from,
  ...props
}: MessageBranchSelectorProps) => {
  const { totalBranches } = useMessageBranch();

  // Don't render if there's only one branch
  if (totalBranches <= 1) {
    return null;
  }

  return (
    <ButtonGroup
      className="[&>*:not(:first-child)]:rounded-l-md [&>*:not(:last-child)]:rounded-r-md"
      orientation="horizontal"
      {...props}
    />
  );
};

export type MessageBranchPreviousProps = ComponentProps<typeof Button>;

export const MessageBranchPrevious = ({
  children,
  ...props
}: MessageBranchPreviousProps) => {
  const { t } = useTranslation("chat");
  const { goToPrevious, totalBranches } = useMessageBranch();

  return (
    <Button
      {...props}
      aria-label={t("message.previousBranch")}
      disabled={totalBranches <= 1}
      onClick={goToPrevious}
      size="icon-sm"
      type="button"
      variant="ghost"
    >
      {children ?? <ChevronLeftIcon size={14} />}
    </Button>
  );
};

export type MessageBranchNextProps = ComponentProps<typeof Button>;

export const MessageBranchNext = ({
  children,
  className,
  ...props
}: MessageBranchNextProps) => {
  const { t } = useTranslation("chat");
  const { goToNext, totalBranches } = useMessageBranch();

  return (
    <Button
      {...props}
      aria-label={t("message.nextBranch")}
      disabled={totalBranches <= 1}
      onClick={goToNext}
      size="icon-sm"
      type="button"
      variant="ghost"
    >
      {children ?? <ChevronRightIcon size={14} />}
    </Button>
  );
};

export type MessageBranchPageProps = HTMLAttributes<HTMLSpanElement>;

export const MessageBranchPage = ({
  className,
  ...props
}: MessageBranchPageProps) => {
  const { currentBranch, totalBranches } = useMessageBranch();

  return (
    <ButtonGroupText
      className={cn(
        "border-none bg-transparent text-muted-foreground shadow-none",
        className,
      )}
      {...props}
    >
      {currentBranch + 1} of {totalBranches}
    </ButtonGroupText>
  );
};

export type MessageResponseProps = ComponentProps<typeof Streamdown>;

export const MessageResponse = memo(
  ({ className, children, ...props }: MessageResponseProps) => (
    <Streamdown
      className={cn(
        "size-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
        streamdownRootClass,
        className,
      )}
      components={streamdownComponents}
      rehypePlugins={safeRehypePlugins}
      remarkPlugins={safeRemarkPlugins}
      {...props}
    >
      {typeof children === "string"
        ? escapeHtmlOutsideCodeBlocks(children)
        : children}
    </Streamdown>
  ),
  // `mode` and `parseIncompleteMarkdown` have to be compared too. The turn end
  // flips `message.isStreaming` to false without touching `children`, so a
  // children-only comparison judged the pair "unchanged" and skipped the
  // re-render -- <Streamdown> then kept parseIncompleteMarkdown={true} on a
  // finished message, remend kept stitching already-complete Markdown, and the
  // raw * _ ~~ stayed on screen until the conversation was rebuilt from history.
  (prevProps, nextProps) =>
    prevProps.children === nextProps.children &&
    prevProps.className === nextProps.className &&
    prevProps.mode === nextProps.mode &&
    prevProps.parseIncompleteMarkdown ===
      nextProps.parseIncompleteMarkdown,
);

MessageResponse.displayName = "MessageResponse";

const FILE_KIND_ICONS: Record<FileKind, { icon: LucideIcon; tint: string }> = {
  pdf: { icon: FileTextIcon, tint: "text-red-500" },
  document: { icon: FileTextIcon, tint: "text-blue-500" },
  spreadsheet: { icon: FileSpreadsheetIcon, tint: "text-green-600" },
  presentation: { icon: PresentationIcon, tint: "text-orange-500" },
  code: { icon: FileCodeIcon, tint: "text-violet-500" },
  data: { icon: FileJsonIcon, tint: "text-amber-500" },
  archive: { icon: FileArchiveIcon, tint: "text-yellow-600" },
  audio: { icon: FileAudioIcon, tint: "text-pink-500" },
  video: { icon: FileVideoIcon, tint: "text-sky-500" },
  image: { icon: FileImageIcon, tint: "text-teal-500" },
  text: { icon: FileTextIcon, tint: "text-muted-foreground" },
  other: { icon: FileIcon, tint: "text-muted-foreground" },
};

export type MessageAttachmentProps = HTMLAttributes<HTMLDivElement> & {
  data: MessageAttachmentPart;
  className?: string;
  onRemove?: () => void;
};

export function MessageAttachment({
  data,
  className,
  onRemove,
  ...props
}: MessageAttachmentProps) {
  const { t } = useTranslation(["chat", "common"]);
  const [isPreviewOpen, setIsPreviewOpen] = useState(false);
  const [textContent, setTextContent] = useState<string | null>(null);
  const isNoPreviewAttachment = (
    attachment: MessageAttachmentPart,
  ): attachment is NoPreviewAttachment =>
    "kind" in attachment && attachment.kind === "nopreview";
  const isVideoNoPreviewAttachment = (
    attachment: MessageAttachmentPart,
  ): attachment is VideoNoPreviewAttachment =>
    "kind" in attachment && attachment.kind === "video-nopreview";
  const isNoPreview = isNoPreviewAttachment(data);
  const isVideoNoPreview = isVideoNoPreviewAttachment(data);
  const filename = data.filename || "";
  let mediaType: string | undefined;
  let url: string | undefined;
  if (!isNoPreview && !isVideoNoPreview) {
    mediaType = data.mediaType;
    url = data.url;
  } else if (isVideoNoPreview) {
    mediaType = data.mediaType;
  }
  const isImage = mediaType?.startsWith("image/") && url;
  const isVideo = mediaType?.startsWith("video/") && url;
  const isText = mediaType?.startsWith("text/") && url;
  const canPreview = (isImage || isVideo || isText) && Boolean(url);
  const attachmentLabel =
    filename ||
    (isImage
      ? t("chat:attachment.image")
      : isVideo || isVideoNoPreview
        ? t("chat:attachment.video")
        : t("chat:attachment.attachment"));
  const typeBadge = isImage
    ? t("chat:attachment.image")
    : isVideo
      ? t("chat:attachment.video")
      : undefined;
  const videoPoster = useVideoThumbnail(isVideo ? url : undefined);
  const extension = fileExtension(filename);
  const { icon: FileKindIcon, tint: fileIconTint } =
    FILE_KIND_ICONS[isVideoNoPreview ? "video" : fileKind(filename, mediaType)];

  // Decode text content from data URL when opening preview
  const handleOpenPreview = () => {
    if (isText && url?.startsWith("data:")) {
      try {
        const base64 = url.split(",")[1];
        const decoded = atob(base64);
        const bytes = Uint8Array.from(decoded, (c) => c.charCodeAt(0));
        setTextContent(new TextDecoder().decode(bytes));
      } catch {
        setTextContent(t("chat:attachment.decodeFailed"));
      }
    }
    setIsPreviewOpen(true);
  };

  return (
    <>
      <div
        className={cn(
          "group relative size-24 overflow-hidden rounded-lg",
          (isImage || isVideo) && "border border-border",
          canPreview ? "cursor-zoom-in" : undefined,
          className,
        )}
        onClick={() => {
          if (canPreview) {
            handleOpenPreview();
          }
        }}
        onKeyDown={(event) => {
          if (!canPreview) {
            return;
          }
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handleOpenPreview();
          }
        }}
        role={canPreview ? "button" : undefined}
        tabIndex={canPreview ? 0 : undefined}
        {...props}
      >
        {isImage ? (
          <>
            <img
              alt={filename || t("chat:attachment.alt")}
              className="size-full object-cover"
              height={160}
              src={url}
              width={160}
            />
            {typeBadge && (
              <span className="pointer-events-none absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white shadow-sm">
                {typeBadge}
              </span>
            )}
            {onRemove && (
              <Button
                aria-label={t("chat:attachment.remove")}
                className="hover-reveal absolute top-2 right-2 size-6 rounded-full bg-background/80 p-0 opacity-0 backdrop-blur-sm transition-opacity hover:bg-background group-hover:opacity-100 [&>svg]:size-3"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove();
                }}
                type="button"
                variant="ghost"
              >
                <XIcon />
                <span className="sr-only">{t("common:actions.remove")}</span>
              </Button>
            )}
          </>
        ) : isVideo ? (
          <>
            <video
              className="size-full object-cover"
              height={160}
              poster={videoPoster ?? undefined}
              preload="metadata"
              src={url}
              width={160}
              muted
              playsInline
            />
            {typeBadge && (
              <span className="pointer-events-none absolute bottom-2 right-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white shadow-sm">
                {typeBadge}
              </span>
            )}
            {onRemove && (
              <Button
                aria-label={t("chat:attachment.remove")}
                className="hover-reveal absolute top-2 right-2 size-6 rounded-full bg-background/80 p-0 opacity-0 backdrop-blur-sm transition-opacity hover:bg-background group-hover:opacity-100 [&>svg]:size-3"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove();
                }}
                type="button"
                variant="ghost"
              >
                <XIcon />
                <span className="sr-only">{t("common:actions.remove")}</span>
              </Button>
            )}
          </>
        ) : (
          <>
            <Tooltip>
              <TooltipTrigger asChild>
                <div className="flex size-full flex-col items-center justify-center gap-2 rounded-lg border border-border bg-muted p-2 text-center">
                  <FileKindIcon className={cn("size-8 shrink-0", fileIconTint)} />
                  <span className="line-clamp-2 w-full text-xs leading-tight break-all text-foreground">
                    {attachmentLabel}
                  </span>
                  {extension && (
                    <span className="rounded bg-background px-1.5 py-0.5 font-mono text-[10px] leading-none text-muted-foreground uppercase">
                      {extension}
                    </span>
                  )}
                </div>
              </TooltipTrigger>
              <TooltipContent>
                <p>{attachmentLabel}</p>
              </TooltipContent>
            </Tooltip>
            {onRemove && (
              <Button
                aria-label={t("chat:attachment.remove")}
                className="hover-reveal size-6 shrink-0 rounded-full p-0 opacity-0 transition-opacity hover:bg-accent group-hover:opacity-100 [&>svg]:size-3"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove();
                }}
                type="button"
                variant="ghost"
              >
                <XIcon />
                <span className="sr-only">{t("common:actions.remove")}</span>
              </Button>
            )}
          </>
        )}
      </div>

      {canPreview ? (
        <Dialog open={isPreviewOpen} onOpenChange={setIsPreviewOpen}>
          <DialogContent
            className="max-w-[min(95vw,1100px)] overflow-hidden p-0 sm:max-w-[min(95vw,1100px)]"
            showCloseButton
          >
            <DialogHeader className={isText ? "p-4 pb-0" : "sr-only"}>
              <DialogTitle>
                {isText ? filename : t("chat:attachment.preview")}
              </DialogTitle>
            </DialogHeader>
            <div className="bg-background">
              {isImage ? (
                <img
                  alt={filename || t("chat:attachment.alt")}
                  className="block max-h-[88vh] w-full object-contain"
                  src={url}
                />
              ) : isVideo ? (
                <video
                  className="block max-h-[88vh] w-full object-contain"
                  src={url}
                  controls
                  poster={videoPoster ?? undefined}
                  autoPlay
                  playsInline
                />
              ) : isText && textContent !== null ? (
                <pre className="max-h-[80vh] overflow-auto p-4 pt-2 text-sm whitespace-pre-wrap wrap-break-word font-mono">
                  {textContent}
                </pre>
              ) : null}
            </div>
          </DialogContent>
        </Dialog>
      ) : null}
    </>
  );
}

export type MessageAttachmentsProps = ComponentProps<"div">;

export function MessageAttachments({
  children,
  className,
  ...props
}: MessageAttachmentsProps) {
  if (!children) {
    return null;
  }

  return (
    <div
      className={cn(
        "ml-auto flex w-fit flex-wrap items-start gap-2",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export type MessageToolbarProps = ComponentProps<"div">;

export const MessageToolbar = ({
  className,
  children,
  ...props
}: MessageToolbarProps) => (
  <div
    className={cn(
      "mt-4 flex w-full items-center justify-between gap-4",
      className,
    )}
    {...props}
  >
    {children}
  </div>
);
