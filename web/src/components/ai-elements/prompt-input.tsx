"use client";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "@/components/ui/input-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { isMacOS } from "@/hooks/utils";
import { cn } from "@/lib/utils";
import { isNewlineShortcut } from "./prompt-input-keys";
import { transcodeHeicFiles } from "@/lib/heic";
import { IMAGE_CONFIG, VIDEO_CONFIG, MEDIA_CONFIG } from "@/config/media";
import { useVideoThumbnail } from "@/hooks/useVideoThumbnail";
import { type DraftBook, switchDraft } from "./prompt-input-drafts";
import {
  type PasteLabels,
  attachmentKind,
  deletionRange,
  expandPastes,
  findAllTokens,
  findFileTokens,
  findTokens,
  makeFileToken,
  makeToken,
  markAttachments,
  nextPasteId,
  referencedIds,
  removeFileTokens,
  shouldUsePlaceholder,
  tokenAtCaret,
} from "./prompt-input-paste";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { useTranslation } from "react-i18next";
import type { ChatStatus, FileUIPart } from "ai";
import {
  CornerDownLeftIcon,
  ImageIcon,
  Loader2Icon,
  MicIcon,
  PaperclipIcon,
  PlusIcon,
  SquareIcon,
  XIcon,
} from "lucide-react";
import { nanoid } from "nanoid";
import {
  type ChangeEvent,
  type ChangeEventHandler,
  Children,
  type ClipboardEventHandler,
  type ComponentProps,
  createContext,
  type FormEvent,
  type FormEventHandler,
  Fragment,
  type HTMLAttributes,
  type KeyboardEventHandler,
  type PropsWithChildren,
  type ReactNode,
  type RefObject,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

// ============================================================================
// Provider Context & Types
// ============================================================================

export type AttachmentsContext = {
  files: (FileUIPart & { id: string })[];
  add: (files: File[] | FileList) => void;
  remove: (id: string) => void;
  clear: () => void;
  openFileDialog: () => void;
  fileInputRef: RefObject<HTMLInputElement | null>;
};

export type TextInputContext = {
  value: string;
  setInput: (v: string) => void;
  clear: () => void;
};

/**
 * Full text of long pastes, held while the composer shows a placeholder for
 * each. Shared by every session's draft, so a placeholder still resolves after
 * switching sessions and back; ids are reused once a message using them is sent.
 */
export type PastesContext = {
  store: (content: string) => number;
  get: (id: number) => string | undefined;
  update: (id: number, content: string) => void;
  release: (ids: Iterable<number>) => void;
};

/** Which attachment each `⟦Image n⟧` / `⟦File n⟧` placeholder stands for. */
export type FileTokensContext = {
  attachmentOf: (tokenId: number) => string | undefined;
};

export type PromptInputControllerProps = {
  textInput: TextInputContext;
  attachments: AttachmentsContext;
  pastes: PastesContext;
  fileTokens: FileTokensContext;
  /** INTERNAL: the textarea placeholders are inserted into. */
  __registerTextarea: (textarea: HTMLTextAreaElement | null) => void;
  /** INTERNAL: Allows PromptInput to register its file textInput + "open" callback */
  __registerFileInput: (
    ref: RefObject<HTMLInputElement | null>,
    open: () => void,
  ) => void;
};

const PromptInputController = createContext<PromptInputControllerProps | null>(
  null,
);
const ProviderAttachmentsContext = createContext<AttachmentsContext | null>(
  null,
);

export const usePromptInputController = () => {
  const ctx = useContext(PromptInputController);
  if (!ctx) {
    throw new Error(
      "Wrap your component inside <PromptInputProvider> to use usePromptInputController().",
    );
  }
  return ctx;
};

// Optional variants (do NOT throw). Useful for dual-mode components.
const useOptionalPromptInputController = () =>
  useContext(PromptInputController);

export const useProviderAttachments = () => {
  const ctx = useContext(ProviderAttachmentsContext);
  if (!ctx) {
    throw new Error(
      "Wrap your component inside <PromptInputProvider> to use useProviderAttachments().",
    );
  }
  return ctx;
};

const useOptionalProviderAttachments = () =>
  useContext(ProviderAttachmentsContext);

export type PromptInputProviderProps = PropsWithChildren<{
  initialInput?: string;
  /**
   * Scopes unsent input. The provider sits above the session switcher, so
   * without a key every session shares one draft; pass the session id and each
   * one keeps its own text and attachments.
   */
  draftKey?: string;
}>;

/**
 * Optional global provider that lifts PromptInput state outside of PromptInput.
 * If you don't use it, PromptInput stays fully self-managed.
 */
export function PromptInputProvider({
  initialInput: initialTextInput = "",
  draftKey = "",
  children,
}: PromptInputProviderProps) {
  // ----- textInput state
  const [textInput, setTextInput] = useState(initialTextInput);
  const clearInput = useCallback(() => setTextInput(""), []);
  const textRef = useRef(textInput);
  textRef.current = textInput;

  // ----- file placeholders: token id -> attachment id, shared by every draft
  // so a placeholder resolves after switching sessions and back.
  const { t: tChat } = useTranslation("chat");
  const fileTokenMap = useRef(new Map<number, string>());
  // Attachments whose placeholder was deleted from the text, set aside rather
  // than dropped: undo brings the placeholder back, and it finds its file
  // here. Tagged with the draft they came from; sending that draft ends them.
  const detached = useRef(
    new Map<number, { draft: string; file: FileUIPart & { id: string } }>(),
  );
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const insertFileTokens = useCallback(
    (items: { id: string; mediaType?: string; filename?: string }[]) => {
      const labels = {
        image: tChat("attachmentToken.image"),
        file: tChat("attachmentToken.file"),
      };
      const text = items
        .map((item) => {
          const tokenId = nextPasteId(fileTokenMap.current.keys());
          fileTokenMap.current.set(tokenId, item.id);
          return makeFileToken(
            tokenId,
            attachmentKind(item.mediaType),
            item.filename ?? "file",
            labels,
          );
        })
        .join(" ");
      const textarea = textareaRef.current;
      if (textarea && document.activeElement === textarea) {
        insertTextAtCaret(textarea, text);
        return;
      }
      // Added from the paperclip or a drop: the caret the person left is
      // still recorded on the textarea, so the file goes where they were.
      const current = textRef.current;
      const start = textarea ? Math.min(textarea.selectionStart, current.length) : current.length;
      const end = textarea ? Math.min(textarea.selectionEnd, current.length) : current.length;
      setTextInput(current.slice(0, start) + text + current.slice(end));
    },
    [tChat],
  );

  // ----- attachments state (global when wrapped)
  const [attachments, setAttachments] = useState<
    (FileUIPart & { id: string })[]
  >([]);

  // ----- per-scope drafts
  const drafts = useRef<DraftBook<FileUIPart & { id: string }>>(new Map());
  const [activeDraftKey, setActiveDraftKey] = useState(draftKey);
  const activeDraftKeyRef = useRef(activeDraftKey);
  activeDraftKeyRef.current = activeDraftKey;
  if (draftKey !== activeDraftKey) {
    // Adjusted during render, not in an effect, so the composer never paints
    // the previous session's draft for a frame. Attachment object URLs are
    // carried over rather than revoked -- the draft is being kept, not sent.
    const restored = switchDraft(
      drafts.current,
      activeDraftKey,
      { text: textInput, files: attachments },
      draftKey,
    );
    setActiveDraftKey(draftKey);
    setTextInput(restored.text);
    setAttachments(restored.files);
  }
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const openRef = useRef<() => void>(() => {});

  const add = useCallback(async (files: File[] | FileList) => {
    const incoming = Array.from(files);
    if (incoming.length === 0) {
      return;
    }

    // Transcode HEIC/HEIF -> JPEG before they ever enter attachments, so
    // both the thumbnail (<img src="blob:...">) and the wire payload stay
    // in a format vision models accept.
    const processed = await transcodeHeicFiles(incoming);
    if (processed.length === 0) {
      return;
    }

    const items = processed.map((file) => ({
      id: nanoid(),
      type: "file" as const,
      url: URL.createObjectURL(file),
      mediaType: file.type,
      filename: file.name,
    }));
    // Placeholders first, so the text already names each file by the time
    // the attachment exists and the two never look out of step.
    insertFileTokens(items);
    setAttachments((prev) => prev.concat(items));
  }, [insertFileTokens]);

  const remove = useCallback((id: string) => {
    // Removing the card removes its placeholder from the text too.
    const tokenIds = new Set<number>();
    for (const [tokenId, attachmentId] of fileTokenMap.current) {
      if (attachmentId === id) tokenIds.add(tokenId);
    }
    if (tokenIds.size > 0) {
      for (const tokenId of tokenIds) fileTokenMap.current.delete(tokenId);
      setTextInput((current) => removeFileTokens(current, tokenIds));
    }
    setAttachments((prev) => {
      const found = prev.find((f) => f.id === id);
      if (found?.url) {
        URL.revokeObjectURL(found.url);
      }
      return prev.filter((f) => f.id !== id);
    });
  }, []);

  const clear = useCallback(() => {
    // The message went out: what this draft set aside will not be undone back.
    for (const [tokenId, entry] of detached.current) {
      if (entry.draft !== activeDraftKeyRef.current) continue;
      if (entry.file.url) URL.revokeObjectURL(entry.file.url);
      detached.current.delete(tokenId);
      fileTokenMap.current.delete(tokenId);
    }
    setAttachments((prev) => {
      const cleared = new Set(prev.map((f) => f.id));
      for (const [tokenId, attachmentId] of fileTokenMap.current) {
        if (cleared.has(attachmentId)) fileTokenMap.current.delete(tokenId);
      }
      for (const f of prev) {
        if (f.url) {
          URL.revokeObjectURL(f.url);
        }
      }
      return [];
    });
  }, []);

  // Deleting a placeholder from the text takes its attachment off the list,
  // into `detached`; the placeholder coming back (undo) puts it back. Only
  // this draft's attachments are looked at; another draft's placeholders are
  // in text that is not on screen.
  useEffect(() => {
    const present = new Set(findFileTokens(textInput).map((token) => token.id));
    const setAside = new Set<string>();
    for (const [tokenId, attachmentId] of fileTokenMap.current) {
      const file = attachments.find((f) => f.id === attachmentId);
      if (file && !present.has(tokenId)) {
        setAside.add(attachmentId);
        detached.current.set(tokenId, { draft: activeDraftKeyRef.current, file });
      }
    }
    const restored: (FileUIPart & { id: string })[] = [];
    for (const [tokenId, entry] of detached.current) {
      if (present.has(tokenId) && !setAside.has(entry.file.id)) {
        restored.push(entry.file);
        detached.current.delete(tokenId);
      }
    }
    if (setAside.size === 0 && restored.length === 0) return;
    setAttachments((prev) =>
      prev
        .filter((f) => !setAside.has(f.id))
        .concat(restored.filter((f) => !prev.some((existing) => existing.id === f.id))),
    );
  }, [textInput, attachments]);

  const openFileDialog = useCallback(() => {
    openRef.current?.();
  }, []);

  const attachmentsContext = useMemo<AttachmentsContext>(
    () => ({
      files: attachments,
      add,
      remove,
      clear,
      openFileDialog,
      fileInputRef,
    }),
    [attachments, add, remove, clear, openFileDialog],
  );

  const __registerFileInput = useCallback(
    (ref: RefObject<HTMLInputElement | null>, open: () => void) => {
      fileInputRef.current = ref.current;
      openRef.current = open;
    },
    [],
  );

  const pasteStore = useRef(new Map<number, string>());
  const pastes = useMemo<PastesContext>(
    () => ({
      store: (content) => {
        const id = nextPasteId(pasteStore.current.keys());
        pasteStore.current.set(id, content);
        return id;
      },
      get: (id) => pasteStore.current.get(id),
      update: (id, content) => {
        pasteStore.current.set(id, content);
      },
      release: (ids) => {
        for (const id of ids) pasteStore.current.delete(id);
      },
    }),
    [],
  );

  const fileTokens = useMemo<FileTokensContext>(
    () => ({ attachmentOf: (tokenId) => fileTokenMap.current.get(tokenId) }),
    [],
  );
  const __registerTextarea = useCallback((textarea: HTMLTextAreaElement | null) => {
    textareaRef.current = textarea;
  }, []);

  const controller = useMemo<PromptInputControllerProps>(
    () => ({
      textInput: {
        value: textInput,
        setInput: setTextInput,
        clear: clearInput,
      },
      attachments: attachmentsContext,
      pastes,
      fileTokens,
      __registerFileInput,
      __registerTextarea,
    }),
    [
      textInput,
      clearInput,
      attachmentsContext,
      pastes,
      fileTokens,
      __registerFileInput,
      __registerTextarea,
    ],
  );

  return (
    <PromptInputController.Provider value={controller}>
      <ProviderAttachmentsContext.Provider value={attachmentsContext}>
        {children}
      </ProviderAttachmentsContext.Provider>
    </PromptInputController.Provider>
  );
}

// ============================================================================
// Component Context & Hooks
// ============================================================================

const LocalAttachmentsContext = createContext<AttachmentsContext | null>(null);

export const usePromptInputAttachments = () => {
  // Dual-mode: prefer provider if present, otherwise use local
  const provider = useOptionalProviderAttachments();
  const local = useContext(LocalAttachmentsContext);
  const context = provider ?? local;
  if (!context) {
    throw new Error(
      "usePromptInputAttachments must be used within a PromptInput or PromptInputProvider",
    );
  }
  return context;
};

export type PromptInputAttachmentProps = HTMLAttributes<HTMLDivElement> & {
  data: FileUIPart & { id: string };
  className?: string;
};

export function PromptInputAttachment({
  data,
  className,
  ...props
}: PromptInputAttachmentProps) {
  const { t } = useTranslation(["chat", "common"]);
  const attachments = usePromptInputAttachments();

  const filename = data.filename || "";

  const isImage = data.mediaType?.startsWith("image/") && data.url;
  const isVideo = data.mediaType?.startsWith("video/") && data.url;

  const attachmentLabel =
    filename ||
    (isImage
      ? t("chat:attachment.image")
      : isVideo
        ? t("chat:attachment.video")
        : t("chat:attachment.attachment"));
  const typeBadge = isImage
    ? t("chat:attachment.image")
    : isVideo
      ? t("chat:attachment.video")
      : undefined;
  const videoPoster = useVideoThumbnail(isVideo ? data.url : undefined);

  if (isImage) {
    return (
      <div
        className={cn("group relative", className)}
        key={data.id}
        {...props}
      >
        <img
          alt={filename || t("chat:attachment.uploadedImage")}
          className="h-14 w-14 rounded-lg border border-border/50 object-cover"
          src={data.url}
        />
        {typeBadge && (
          <span className="pointer-events-none absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[9px] font-semibold leading-none text-white shadow-sm">
            {typeBadge}
          </span>
        )}
        <Button
          aria-label={t("chat:attachment.remove")}
          className="hover-reveal absolute -right-1.5 -top-1.5 size-5 cursor-pointer rounded-full bg-background p-0 opacity-0 shadow-sm transition-opacity group-hover:opacity-100 [&>svg]:size-3"
          onClick={(e) => {
            e.stopPropagation();
            attachments.remove(data.id);
          }}
          type="button"
          variant="outline"
        >
          <XIcon />
          <span className="sr-only">{t("common:actions.remove")}</span>
        </Button>
      </div>
    );
  }

  if (isVideo) {
    return (
      <div
        className={cn("group relative", className)}
        key={data.id}
        {...props}
      >
        <video
          className="h-14 w-14 rounded-lg border border-border/50 object-cover"
          poster={videoPoster ?? undefined}
          preload="metadata"
          src={data.url}
          muted
          playsInline
        />
        {typeBadge && (
          <span className="pointer-events-none absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[9px] font-semibold leading-none text-white shadow-sm">
            {typeBadge}
          </span>
        )}
        <Button
          aria-label={t("chat:attachment.remove")}
          className="hover-reveal absolute -right-1.5 -top-1.5 size-5 cursor-pointer rounded-full bg-background p-0 opacity-0 shadow-sm transition-opacity group-hover:opacity-100 [&>svg]:size-3"
          onClick={(e) => {
            e.stopPropagation();
            attachments.remove(data.id);
          }}
          type="button"
          variant="outline"
        >
          <XIcon />
          <span className="sr-only">{t("common:actions.remove")}</span>
        </Button>
      </div>
    );
  }

  return (
    <PromptInputHoverCard>
      <HoverCardTrigger asChild>
        <div
          className={cn(
            "group relative flex h-8 cursor-default select-none items-center gap-1.5 rounded-md border border-border px-1.5 font-medium text-sm transition-all hover:bg-accent hover:text-accent-foreground dark:hover:bg-accent/50",
            className,
          )}
          key={data.id}
          {...props}
        >
          <div className="relative size-5 shrink-0">
            <div className="absolute inset-0 flex size-5 items-center justify-center overflow-hidden rounded bg-background transition-opacity group-hover:opacity-0">
              <div className="flex size-5 items-center justify-center text-muted-foreground">
                <PaperclipIcon className="size-3" />
              </div>
            </div>
            <Button
              aria-label={t("chat:attachment.remove")}
              className="hover-reveal absolute inset-0 size-5 cursor-pointer rounded p-0 opacity-0 transition-opacity group-hover:pointer-events-auto group-hover:opacity-100 [&>svg]:size-2.5"
              onClick={(e) => {
                e.stopPropagation();
                attachments.remove(data.id);
              }}
              type="button"
              variant="ghost"
            >
              <XIcon />
              <span className="sr-only">{t("common:actions.remove")}</span>
            </Button>
          </div>

          <span className="flex-1 truncate">{attachmentLabel}</span>
        </div>
      </HoverCardTrigger>
      <PromptInputHoverCardContent className="w-auto p-2">
        <div className="w-auto space-y-3">
          <div className="flex items-center gap-2.5">
            <div className="min-w-0 flex-1 space-y-1 px-0.5">
              <h4 className="truncate font-semibold text-sm leading-none">
                {attachmentLabel}
              </h4>
              {data.mediaType && (
                <p className="truncate font-mono text-muted-foreground text-xs">
                  {data.mediaType}
                </p>
              )}
            </div>
          </div>
        </div>
      </PromptInputHoverCardContent>
    </PromptInputHoverCard>
  );
}

export type PromptInputAttachmentsProps = Omit<
  HTMLAttributes<HTMLDivElement>,
  "children"
> & {
  children: (attachment: FileUIPart & { id: string }) => ReactNode;
};

export function PromptInputAttachments({
  children,
  className,
  ...props
}: PromptInputAttachmentsProps) {
  const attachments = usePromptInputAttachments();

  if (!attachments.files.length) {
    return null;
  }

  return (
    <div
      className={cn("flex flex-wrap items-start gap-2 p-3 w-full", className)}
      {...props}
    >
      {attachments.files.map((file) => (
        <Fragment key={file.id}>{children(file)}</Fragment>
      ))}
    </div>
  );
}

export type PromptInputActionAddAttachmentsProps = ComponentProps<
  typeof DropdownMenuItem
> & {
  label?: string;
};

export const PromptInputActionAddAttachments = ({
  label,
  ...props
}: PromptInputActionAddAttachmentsProps) => {
  const { t } = useTranslation("chat");
  const attachments = usePromptInputAttachments();

  return (
    <DropdownMenuItem
      {...props}
      onSelect={(e) => {
        e.preventDefault();
        attachments.openFileDialog();
      }}
    >
      <ImageIcon className="mr-2 size-4" /> {label ?? t("attachment.addPhotosOrFiles")}
    </DropdownMenuItem>
  );
};

export type PromptInputMessage = {
  text: string;
  files: FileUIPart[];
};

export type PromptInputProps = Omit<
  HTMLAttributes<HTMLFormElement>,
  "onSubmit" | "onError"
> & {
  accept?: string; // e.g., "image/*" or leave undefined for any
  multiple?: boolean;
  // When true, accepts drops anywhere on document. Default false (opt-in).
  globalDrop?: boolean;
  // Render a hidden input with given name and keep it in sync for native form posts. Default false.
  syncHiddenInput?: boolean;
  // Minimal constraints
  maxFiles?: number;
  maxFileSize?: number; // bytes
  onError?: (err: {
    code: "max_files" | "max_file_size" | "accept";
    message: string;
  }) => void;
  onSubmit: (
    message: PromptInputMessage,
    event: FormEvent<HTMLFormElement>,
  ) => void | Promise<void>;
};

export const PromptInput = ({
  className,
  accept,
  multiple,
  globalDrop,
  syncHiddenInput,
  maxFiles,
  maxFileSize,
  onError,
  onSubmit,
  children,
  ...props
}: PromptInputProps) => {
  const { t } = useTranslation("chat");
  // Try to use a provider controller if present
  const controller = useOptionalPromptInputController();
  const usingProvider = !!controller;

  // Refs
  const inputRef = useRef<HTMLInputElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);

  // ----- Local attachments (only used when no provider)
  const [items, setItems] = useState<(FileUIPart & { id: string })[]>([]);
  const files = usingProvider ? controller.attachments.files : items;

  const openFileDialogLocal = useCallback(() => {
    inputRef.current?.click();
  }, []);

  const matchesAccept = useCallback(
    (f: File) => {
      if (!accept || accept.trim() === "") {
        return true;
      }
      const rules = accept
        .split(",")
        .map((rule) => rule.trim().toLowerCase())
        .filter(Boolean);
      // "*" and "*/*" (possibly alongside other rules) mean "any file", as the
      // browser treats them in an <input accept> attribute. A literal "*"
      // matches no MIME type, so without this branch every drop is rejected
      // (regression introduced by 2ab48a8c).
      if (rules.includes("*") || rules.includes("*/*")) {
        return true;
      }
      const name = f.name.toLowerCase();
      return rules.some((rule) => {
        if (rule.startsWith(".")) {
          return name.endsWith(rule);
        }
        if (rule.endsWith("/*")) {
          return f.type.startsWith(rule.slice(0, -1));
        }
        return f.type === rule;
      });
    },
    [accept],
  );

  const validateMediaFile = useCallback(
    (file: File): { valid: boolean; error?: string } => {
      const isImage = file.type.startsWith("image/");
      const isVideo = file.type.startsWith("video/");

      if (isImage) {
        if (!IMAGE_CONFIG.allowedTypes.includes(file.type as typeof IMAGE_CONFIG.allowedTypes[number])) {
          return {
            valid: false,
            error: t("upload.unsupportedImageType", { type: file.type }),
          };
        }
        if (file.size > IMAGE_CONFIG.maxSizeBytes) {
          return {
            valid: false,
            error: t("upload.imageTooLarge", {
              size: (file.size / 1024 / 1024).toFixed(1),
              max: IMAGE_CONFIG.maxSizeBytes / 1024 / 1024,
            }),
          };
        }
      } else if (isVideo) {
        if (!VIDEO_CONFIG.allowedTypes.includes(file.type as typeof VIDEO_CONFIG.allowedTypes[number])) {
          return {
            valid: false,
            error: t("upload.unsupportedVideoType", { type: file.type }),
          };
        }
        if (file.size > VIDEO_CONFIG.maxSizeBytes) {
          return {
            valid: false,
            error: t("upload.videoTooLarge", {
              size: (file.size / 1024 / 1024).toFixed(1),
              max: VIDEO_CONFIG.maxSizeBytes / 1024 / 1024,
            }),
          };
        }
      }
      return { valid: true };
    },
    [t],
  );

  /**
   * Validates and filters files with unified error handling.
   * Returns validated files and optionally reports the first error encountered.
   */
  const validateAndFilterFiles = useCallback(
    (fileList: File[] | FileList): File[] => {
      const incoming = Array.from(fileList);

      // Filter by accept type
      const accepted = incoming.filter((f) => matchesAccept(f));
      if (incoming.length && accepted.length === 0) {
        onError?.({
          code: "accept",
          message: t("upload.noAcceptedTypes"),
        });
        return [];
      }

      // Validate media files (images/videos) and collect first error
      const validatedFiles: File[] = [];
      let firstValidationError: string | null = null;

      for (const file of accepted) {
        const validation = validateMediaFile(file);
        if (!validation.valid) {
          if (!firstValidationError) {
            firstValidationError =
              validation.error ?? t("upload.validationFailed");
          }
        } else {
          validatedFiles.push(file);
        }
      }

      // Report first validation error if any files failed
      if (firstValidationError && validatedFiles.length < accepted.length) {
        const failedCount = accepted.length - validatedFiles.length;
        const message = failedCount > 1
          ? t("upload.manyFailed", {
              count: failedCount,
              error: firstValidationError,
            })
          : firstValidationError;
        onError?.({
          code: "max_file_size",
          message,
        });
      }

      if (validatedFiles.length === 0) {
        return [];
      }

      // Filter by maxFileSize (if specified)
      if (maxFileSize) {
        const sized = validatedFiles.filter((f) => f.size <= maxFileSize);
        if (sized.length === 0) {
          onError?.({
            code: "max_file_size",
            message: t("upload.allTooLarge"),
          });
          return [];
        }
        return sized;
      }

      return validatedFiles;
    },
    [matchesAccept, validateMediaFile, maxFileSize, onError, t],
  );

  const addLocal = useCallback(
    async (fileList: File[] | FileList) => {
      const validatedFiles = validateAndFilterFiles(fileList);
      if (validatedFiles.length === 0) {
        return;
      }

      // Transcode HEIC/HEIF -> JPEG up front. Validation already happened
      // against the original MIME (which still lists image/heic*), and the
      // size cap is unlikely to flip after re-encoding, so it's safe to
      // convert post-validation.
      const transcoded = await transcodeHeicFiles(validatedFiles);
      if (transcoded.length === 0) {
        return;
      }

      setItems((prev) => {
        const effectiveMaxFiles = maxFiles ?? MEDIA_CONFIG.maxCount;
        const capacity = Math.max(0, effectiveMaxFiles - prev.length);
        const capped = transcoded.slice(0, capacity);

        if (transcoded.length > capacity) {
          onError?.({
            code: "max_files",
            message: t("upload.tooMany", { count: effectiveMaxFiles }),
          });
        }

        const next: (FileUIPart & { id: string })[] = [];
        for (const file of capped) {
          next.push({
            id: nanoid(),
            type: "file",
            url: URL.createObjectURL(file),
            mediaType: file.type,
            filename: file.name,
          });
        }
        return prev.concat(next);
      });
    },
    [validateAndFilterFiles, maxFiles, onError, t],
  );

  const addWithValidation = useCallback(
    (fileList: File[] | FileList) => {
      const validatedFiles = validateAndFilterFiles(fileList);
      if (validatedFiles.length === 0) {
        return;
      }

      // Check max files limit
      const currentCount = controller?.attachments.files.length ?? 0;
      const effectiveMaxFiles = maxFiles ?? MEDIA_CONFIG.maxCount;
      const capacity = Math.max(0, effectiveMaxFiles - currentCount);
      const capped = validatedFiles.slice(0, capacity);

      if (validatedFiles.length > capacity) {
        onError?.({
          code: "max_files",
          message: t("upload.tooMany", { count: effectiveMaxFiles }),
        });
      }

      if (capped.length > 0) {
        controller?.attachments.add(capped);
      }
    },
    [controller, maxFiles, onError, validateAndFilterFiles, t],
  );

  const add = usingProvider ? addWithValidation : addLocal;

  const remove = usingProvider
    ? (id: string) => controller.attachments.remove(id)
    : (id: string) =>
        setItems((prev) => {
          const found = prev.find((file) => file.id === id);
          if (found?.url) {
            URL.revokeObjectURL(found.url);
          }
          return prev.filter((file) => file.id !== id);
        });

  const clear = usingProvider
    ? () => controller.attachments.clear()
    : () =>
        setItems((prev) => {
          for (const file of prev) {
            if (file.url) {
              URL.revokeObjectURL(file.url);
            }
          }
          return [];
        });

  const openFileDialog = usingProvider
    ? () => controller.attachments.openFileDialog()
    : openFileDialogLocal;

  // Let provider know about our hidden file input so external menus can call openFileDialog()
  useEffect(() => {
    if (!usingProvider) return;
    controller.__registerFileInput(inputRef, () => inputRef.current?.click());
  }, [usingProvider, controller]);

  // Note: File input cannot be programmatically set for security reasons
  // The syncHiddenInput prop is no longer functional
  useEffect(() => {
    if (syncHiddenInput && inputRef.current && files.length === 0) {
      inputRef.current.value = "";
    }
  }, [files, syncHiddenInput]);

  // Attach drop handlers on nearest form and document (opt-in)
  useEffect(() => {
    const form = formRef.current;
    if (!form) return;

    const onDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
    };
    const onDrop = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        add(e.dataTransfer.files);
      }
    };
    form.addEventListener("dragover", onDragOver);
    form.addEventListener("drop", onDrop);
    return () => {
      form.removeEventListener("dragover", onDragOver);
      form.removeEventListener("drop", onDrop);
    };
  }, [add]);

  useEffect(() => {
    if (!globalDrop) return;

    const onDragOver = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
    };
    const onDrop = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes("Files")) {
        e.preventDefault();
      }
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        add(e.dataTransfer.files);
      }
    };
    document.addEventListener("dragover", onDragOver);
    document.addEventListener("drop", onDrop);
    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("drop", onDrop);
    };
  }, [add, globalDrop]);

  useEffect(
    () => () => {
      if (!usingProvider) {
        for (const f of files) {
          if (f.url) URL.revokeObjectURL(f.url);
        }
      }
    },
    [usingProvider, files],
  );

  const handleChange: ChangeEventHandler<HTMLInputElement> = (event) => {
    const input = event.currentTarget;
    if (input.files) {
      const selected = Array.from(input.files);
      input.value = "";
      add(selected);
    }
  };

  const convertBlobUrlToDataUrl = async (url: string): Promise<string> => {
    const response = await fetch(url);
    const blob = await response.blob();
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onloadend = () => resolve(reader.result as string);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  };

  const ctx = useMemo<AttachmentsContext>(
    () => ({
      files: files.map((item) => ({ ...item, id: item.id })),
      add,
      remove,
      clear,
      openFileDialog,
      fileInputRef: inputRef,
    }),
    [files, add, remove, clear, openFileDialog],
  );

  const handleSubmit: FormEventHandler<HTMLFormElement> = (event) => {
    event.preventDefault();

    const form = event.currentTarget;
    const written = usingProvider
      ? controller.textInput.value
      : (() => {
          const formData = new FormData(form);
          return (formData.get("message") as string) || "";
        })();
    // Placeholders go out as the text they stand for, where they stand; a file
    // placeholder as a marker naming its place among this message's files.
    const positionOf = new Map(files.map((file, index) => [file.id, index]));
    const text = usingProvider
      ? markAttachments(expandPastes(written, controller.pastes.get), (tokenId) => {
          const attachmentId = controller.fileTokens.attachmentOf(tokenId);
          return attachmentId === undefined ? undefined : positionOf.get(attachmentId);
        })
      : written;
    const releasePastes = () => {
      if (usingProvider) controller.pastes.release(referencedIds(written));
    };

    // Reset form immediately after capturing text to avoid race condition
    // where user input during async blob conversion would be lost
    if (!usingProvider) {
      form.reset();
    }

    // Convert blob URLs to data URLs asynchronously
    Promise.all(
      files.map(async ({ id, ...item }) => {
        if (item.url && item.url.startsWith("blob:")) {
          return {
            ...item,
            url: await convertBlobUrlToDataUrl(item.url),
          };
        }
        return item;
      }),
    )
      .then((convertedFiles: FileUIPart[]) => {
        try {
          const result = onSubmit({ text, files: convertedFiles }, event);

          // Handle both sync and async onSubmit
          if (result instanceof Promise) {
            result
              .then(() => {
                clear();
                if (usingProvider) {
                  controller.textInput.clear();
                }
                releasePastes();
              })
              .catch(() => {
                // Don't clear on error - user may want to retry
              });
          } else {
            // Sync function completed without throwing, clear attachments
            clear();
            if (usingProvider) {
              controller.textInput.clear();
            }
            releasePastes();
          }
        } catch {
          // Don't clear on error - user may want to retry
        }
      })
      .catch((error) => {
        console.error("[PromptInput] Failed to convert files:", error);
        onError?.({
          code: "max_file_size",
          message: t("upload.processFailed"),
        });
      });
  };

  // Render with or without local provider
  const inner = (
    <>
      <input
        accept={accept}
        aria-label={t("upload.uploadFiles")}
        className="hidden"
        multiple={multiple}
        onChange={handleChange}
        ref={inputRef}
        title={t("upload.uploadFiles")}
        type="file"
      />
      <form
        className={cn("w-full", className)}
        autoComplete="off"
        onSubmit={handleSubmit}
        ref={formRef}
        {...props}
      >
        <InputGroup className="overflow-visible">{children}</InputGroup>
      </form>
    </>
  );

  return usingProvider ? (
    inner
  ) : (
    <LocalAttachmentsContext.Provider value={ctx}>
      {inner}
    </LocalAttachmentsContext.Provider>
  );
};

export type PromptInputBodyProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputBody = ({
  className,
  ...props
}: PromptInputBodyProps) => (
  <div className={cn("contents", className)} {...props} />
);

export type PromptInputTextareaProps = ComponentProps<
  typeof InputGroupTextarea
>;

const insertNewlineAtCaret = (textarea: HTMLTextAreaElement) => {
  // insertText keeps the edit on the undo stack and fires a native input
  // event, so a controlled textarea's onChange sees the new value.
  if (document.execCommand("insertText", false, "\n")) {
    return;
  }
  textarea.setRangeText(
    "\n",
    textarea.selectionStart,
    textarea.selectionEnd,
    "end",
  );
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
};

const insertTextAtCaret = (textarea: HTMLTextAreaElement, text: string) => {
  // Same route as a newline: kept on the undo stack, and seen by onChange.
  if (document.execCommand("insertText", false, text)) {
    return;
  }
  textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, "end");
  textarea.dispatchEvent(new Event("input", { bubbles: true }));
};

// What the highlight layer copies so its text wraps exactly like the textarea's.
const MIRRORED_STYLES = [
  "boxSizing",
  "width",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "fontFamily",
  "fontSize",
  "fontWeight",
  "fontStyle",
  "letterSpacing",
  "lineHeight",
  "textIndent",
  "textTransform",
  "wordSpacing",
  "tabSize",
] as const;

/**
 * Paints a tint behind each placeholder. It sits under a transparent-backed
 * textarea and draws its own copy of the text invisibly, so only the tint
 * shows; the textarea keeps its own text, caret and IME composition.
 */
function PasteHighlight({
  textarea,
  value,
}: {
  textarea: HTMLTextAreaElement | null;
  value: string;
}) {
  const layer = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const target = layer.current;
    if (!(textarea && target)) return;
    const sync = () => {
      const computed = window.getComputedStyle(textarea);
      for (const name of MIRRORED_STYLES) {
        target.style[name] = computed[name];
      }
      target.style.height = `${textarea.clientHeight}px`;
      target.scrollTop = textarea.scrollTop;
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(textarea);
    textarea.addEventListener("scroll", sync);
    return () => {
      observer.disconnect();
      textarea.removeEventListener("scroll", sync);
    };
  }, [textarea, value]);

  const tokens = findAllTokens(value);
  const parts: ReactNode[] = [];
  let last = 0;
  for (const token of tokens) {
    parts.push(value.slice(last, token.start));
    parts.push(
      <mark className="rounded-sm bg-primary/15 text-transparent" key={token.start}>
        {value.slice(token.start, token.end)}
      </mark>,
    );
    last = token.end;
  }
  // A trailing newline needs something after it, or the last line collapses.
  parts.push(`${value.slice(last)}​`);

  return (
    <div
      aria-hidden
      className="pointer-events-none absolute top-0 left-0 overflow-hidden whitespace-pre-wrap break-words border-transparent text-transparent"
      ref={layer}
    >
      {parts}
    </div>
  );
}

export const PromptInputTextarea = forwardRef<
  HTMLTextAreaElement,
  PromptInputTextareaProps
>(
  (
    {
      onChange,
      onKeyDown,
      onPaste,
      onSelect,
      onClick,
      onKeyUp,
      className,
      placeholder,
      ...props
    },
    ref,
  ) => {
    const controller = useOptionalPromptInputController();
    const attachments = usePromptInputAttachments();
    const localAttachments = useContext(LocalAttachmentsContext);
    const { t } = useTranslation("chat");
    const [isComposing, setIsComposing] = useState(false);
    const [element, setElement] = useState<HTMLTextAreaElement | null>(null);
    const [caret, setCaret] = useState<number | null>(null);
    const [viewing, setViewing] = useState<{ id: number; content: string } | null>(null);

    const registerTextarea = controller?.__registerTextarea;
    const setRefs = useCallback(
      (node: HTMLTextAreaElement | null) => {
        setElement(node);
        registerTextarea?.(node);
        if (typeof ref === "function") ref(node);
        else if (ref) ref.current = node;
      },
      [ref, registerTextarea],
    );

    const labels: PasteLabels = {
      noun: t("paste.noun"),
      lines: (count) => t("paste.lines", { count }),
      chars: (count) => t("paste.chars", { count }),
    };

    const value = controller?.textInput.value ?? "";
    const hasTokens = controller !== null && findAllTokens(value).length > 0;
    const activeToken =
      controller && caret !== null && document.activeElement === element
        ? tokenAtCaret(value, caret)
        : null;
    const activeContent = activeToken ? controller?.pastes.get(activeToken.id) : undefined;

    const trackCaret = (textarea: HTMLTextAreaElement) => {
      setCaret(textarea.selectionStart === textarea.selectionEnd ? textarea.selectionStart : null);
    };

    const handleKeyDown: KeyboardEventHandler<HTMLTextAreaElement> = (e) => {
      onKeyDown?.(e);
      if (e.defaultPrevented) {
        return;
      }

      if (
        controller &&
        (e.key === "Backspace" || e.key === "Delete") &&
        !(isComposing || e.nativeEvent.isComposing)
      ) {
        const textarea = e.currentTarget;
        const range = deletionRange(
          textarea.value,
          textarea.selectionStart,
          textarea.selectionEnd,
          e.key,
        );
        if (range) {
          // Widen the selection and let the browser delete it, so the
          // deletion is one undo step like any other.
          textarea.setSelectionRange(range.start, range.end);
          return;
        }
      }

      if (e.key === "Enter") {
        if (isComposing || e.nativeEvent.isComposing) {
          return;
        }
        if (e.shiftKey) {
          return;
        }
        // Cmd+Enter on macOS, Ctrl+Enter elsewhere, inserts a newline. The
        // browser does nothing with a modified Enter on its own, so without
        // this the shortcut neither submitted nor broke the line.
        if (isNewlineShortcut(e, isMacOS())) {
          e.preventDefault();
          insertNewlineAtCaret(e.currentTarget);
          return;
        }
        // The other modifier still never submits.
        if (e.ctrlKey || e.metaKey) {
          return;
        }
        e.preventDefault();

        // Check if the submit button is disabled before submitting
        const form = e.currentTarget.form;
        const submitButton = form?.querySelector(
          'button[type="submit"]',
        ) as HTMLButtonElement | null;
        if (submitButton?.disabled) {
          return;
        }

        form?.requestSubmit();
      }

      // Remove last attachment when Backspace is pressed and textarea is empty
      if (
        e.key === "Backspace" &&
        e.currentTarget.value === "" &&
        attachments.files.length > 0
      ) {
        e.preventDefault();
        const lastAttachment = attachments.files.at(-1);
        if (lastAttachment) {
          attachments.remove(lastAttachment.id);
        }
      }
    };

    const handlePaste: ClipboardEventHandler<HTMLTextAreaElement> = (event) => {
      onPaste?.(event);
      if (event.defaultPrevented) {
        return;
      }

      const clipboard = event.clipboardData;
      if (!clipboard) {
        return;
      }

      // Browsers disagree on where a pasted file shows up: some fill
      // `files`, some only `items`. Take whichever has them.
      const files: File[] = Array.from(clipboard.files ?? []);
      if (files.length === 0) {
        for (const item of Array.from(clipboard.items ?? [])) {
          if (item.kind === "file") {
            const file = item.getAsFile();
            if (file) {
              files.push(file);
            }
          }
        }
      }

      if (files.length > 0) {
        event.preventDefault();
        (localAttachments?.add ?? attachments.add)(files);
        return;
      }

      // A file copied in Finder reaches some browsers as a `file://` link
      // with no contents a page is allowed to read. Say so instead of pasting
      // a path the server cannot open.
      const links = clipboard.getData("text/uri-list");
      if (links.split(/\r?\n/).some((line) => line.trim().startsWith("file://"))) {
        event.preventDefault();
        toast.info(t("attachmentToken.pasteUnsupportedTitle"), {
          description: t("attachmentToken.pasteUnsupported"),
        });
        return;
      }

      const pasted = event.clipboardData.getData("text/plain");
      if (controller && shouldUsePlaceholder(pasted)) {
        event.preventDefault();
        const id = controller.pastes.store(pasted);
        insertTextAtCaret(event.currentTarget, makeToken(id, pasted, labels));
      }
    };

    const replaceToken = (replacement: string) => {
      if (!(controller && activeToken)) return;
      const next =
        value.slice(0, activeToken.start) + replacement + value.slice(activeToken.end);
      controller.textInput.setInput(next);
    };

    const expandActive = () => {
      if (!(controller && activeToken && activeContent !== undefined)) return;
      replaceToken(activeContent);
      if (!referencedIds(value.slice(0, activeToken.start) + value.slice(activeToken.end)).has(activeToken.id)) {
        controller.pastes.release([activeToken.id]);
      }
      setCaret(null);
    };

    const saveViewing = () => {
      if (!(controller && viewing)) return;
      controller.pastes.update(viewing.id, viewing.content);
      // Every placeholder for this paste gets the new size in its label.
      const label = makeToken(viewing.id, viewing.content, labels);
      let next = value;
      for (const token of findTokens(value).reverse()) {
        if (token.id === viewing.id) {
          next = next.slice(0, token.start) + label + next.slice(token.end);
        }
      }
      controller.textInput.setInput(next);
      setViewing(null);
    };

    const controlledProps = controller
      ? {
          value: controller.textInput.value,
          onChange: (e: ChangeEvent<HTMLTextAreaElement>) => {
            controller.textInput.setInput(e.currentTarget.value);
            trackCaret(e.currentTarget);
            onChange?.(e);
          },
        }
      : {
          onChange,
        };

    return (
      <>
        {hasTokens ? <PasteHighlight textarea={element} value={value} /> : null}
        <InputGroupTextarea
          ref={setRefs}
          className={cn(
            "field-sizing-content max-h-48 min-h-16",
            hasTokens && "relative",
            className,
          )}
          autoComplete="off"
          name="message"
          onBlur={() => setIsComposing(false)}
          onCompositionEnd={() => setIsComposing(false)}
          onCompositionStart={() => setIsComposing(true)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onSelect={(e) => {
            trackCaret(e.currentTarget);
            onSelect?.(e);
          }}
          onClick={(e) => {
            trackCaret(e.currentTarget);
            onClick?.(e);
          }}
          onKeyUp={(e) => {
            trackCaret(e.currentTarget);
            onKeyUp?.(e);
          }}
          placeholder={placeholder ?? t("composer.placeholderGeneric")}
          {...props}
          {...controlledProps}
        />
        {activeToken && activeContent !== undefined ? (
          <div className="flex items-center gap-3 px-3 pb-1 text-xs text-muted-foreground">
            <span className="truncate">
              {makeToken(activeToken.id, activeContent, labels)}
            </span>
            <button
              className="shrink-0 text-primary hover:underline"
              // Keep focus (and the caret) in the textarea.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => setViewing({ id: activeToken.id, content: activeContent })}
              type="button"
            >
              {t("paste.view")}
            </button>
            <button
              className="shrink-0 text-primary hover:underline"
              onMouseDown={(e) => e.preventDefault()}
              onClick={expandActive}
              type="button"
            >
              {t("paste.expand")}
            </button>
          </div>
        ) : null}
        <Dialog open={viewing !== null} onOpenChange={(open) => !open && setViewing(null)}>
          <DialogContent className="sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>{t("paste.editTitle", { id: viewing?.id })}</DialogTitle>
            </DialogHeader>
            <Textarea
              className="max-h-[60vh] min-h-64 font-mono text-sm"
              onChange={(e) => setViewing((current) => current && { ...current, content: e.target.value })}
              value={viewing?.content ?? ""}
            />
            <DialogFooter>
              <Button onClick={() => setViewing(null)} type="button" variant="outline">
                {t("paste.cancel")}
              </Button>
              <Button disabled={!viewing?.content.trim()} onClick={saveViewing} type="button">
                {t("paste.save")}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </>
    );
  },
);

export type PromptInputHeaderProps = Omit<
  ComponentProps<typeof InputGroupAddon>,
  "align"
>;

export const PromptInputHeader = ({
  className,
  ...props
}: PromptInputHeaderProps) => (
  <InputGroupAddon
    align="block-end"
    className={cn("order-first flex-wrap gap-1", className)}
    {...props}
  />
);

export type PromptInputFooterProps = Omit<
  ComponentProps<typeof InputGroupAddon>,
  "align"
>;

export const PromptInputFooter = ({
  className,
  ...props
}: PromptInputFooterProps) => (
  <InputGroupAddon
    align="block-end"
    className={cn("justify-between gap-1", className)}
    {...props}
  />
);

export type PromptInputToolsProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTools = ({
  className,
  ...props
}: PromptInputToolsProps) => (
  <div className={cn("flex items-center gap-1", className)} {...props} />
);

export type PromptInputButtonProps = ComponentProps<typeof InputGroupButton>;

export const PromptInputButton = ({
  variant = "ghost",
  className,
  size,
  ...props
}: PromptInputButtonProps) => {
  const newSize =
    size ?? (Children.count(props.children) > 1 ? "sm" : "icon-sm");

  return (
    <InputGroupButton
      className={cn(className)}
      size={newSize}
      type="button"
      variant={variant}
      {...props}
    />
  );
};

export type PromptInputActionMenuProps = ComponentProps<typeof DropdownMenu>;
export const PromptInputActionMenu = (props: PromptInputActionMenuProps) => (
  <DropdownMenu {...props} />
);

export type PromptInputActionMenuTriggerProps = PromptInputButtonProps;

export const PromptInputActionMenuTrigger = ({
  className,
  children,
  ...props
}: PromptInputActionMenuTriggerProps) => (
  <DropdownMenuTrigger asChild>
    <PromptInputButton className={className} {...props}>
      {children ?? <PlusIcon className="size-4" />}
    </PromptInputButton>
  </DropdownMenuTrigger>
);

export type PromptInputActionMenuContentProps = ComponentProps<
  typeof DropdownMenuContent
>;
export const PromptInputActionMenuContent = ({
  className,
  ...props
}: PromptInputActionMenuContentProps) => (
  <DropdownMenuContent align="start" className={cn(className)} {...props} />
);

export type PromptInputActionMenuItemProps = ComponentProps<
  typeof DropdownMenuItem
>;
export const PromptInputActionMenuItem = ({
  className,
  ...props
}: PromptInputActionMenuItemProps) => (
  <DropdownMenuItem className={cn(className)} {...props} />
);

// Note: Actions that perform side-effects (like opening a file dialog)
// are provided in opt-in modules (e.g., prompt-input-attachments).

export type PromptInputSubmitProps = ComponentProps<typeof InputGroupButton> & {
  status?: ChatStatus;
};

export const PromptInputSubmit = ({
  className,
  variant = "default",
  size = "icon-sm",
  status,
  children,
  ...props
}: PromptInputSubmitProps) => {
  const { t } = useTranslation("chat");
  let Icon = <CornerDownLeftIcon className="size-4" />;

  if (status === "submitted") {
    Icon = <Loader2Icon className="size-4 animate-spin" />;
  } else if (status === "streaming") {
    Icon = <SquareIcon className="size-4" />;
  } else if (status === "error") {
    Icon = <XIcon className="size-4" />;
  }

  return (
    <InputGroupButton
      aria-label={t("composer.submit")}
      className={cn(className)}
      size={size}
      type="submit"
      variant={variant}
      {...props}
    >
      {children ?? Icon}
    </InputGroupButton>
  );
};

interface SpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  onstart: ((this: SpeechRecognition, ev: Event) => void) | null;
  onend: ((this: SpeechRecognition, ev: Event) => void) | null;
  onresult:
    | ((this: SpeechRecognition, ev: SpeechRecognitionEvent) => void)
    | null;
  onerror:
    | ((this: SpeechRecognition, ev: SpeechRecognitionErrorEvent) => void)
    | null;
}

interface SpeechRecognitionEvent extends Event {
  results: SpeechRecognitionResultList;
  resultIndex: number;
}

type SpeechRecognitionResultList = {
  readonly length: number;
  item(index: number): SpeechRecognitionResult;
  [index: number]: SpeechRecognitionResult;
};

type SpeechRecognitionResult = {
  readonly length: number;
  item(index: number): SpeechRecognitionAlternative;
  [index: number]: SpeechRecognitionAlternative;
  isFinal: boolean;
};

type SpeechRecognitionAlternative = {
  transcript: string;
  confidence: number;
};

interface SpeechRecognitionErrorEvent extends Event {
  error: string;
}

declare global {
  interface Window {
    SpeechRecognition: {
      new (): SpeechRecognition;
    };
    webkitSpeechRecognition: {
      new (): SpeechRecognition;
    };
  }
}

export type PromptInputSpeechButtonProps = ComponentProps<
  typeof PromptInputButton
> & {
  textareaRef?: RefObject<HTMLTextAreaElement | null>;
  onTranscriptionChange?: (text: string) => void;
};

export const PromptInputSpeechButton = ({
  className,
  textareaRef,
  onTranscriptionChange,
  ...props
}: PromptInputSpeechButtonProps) => {
  const [isListening, setIsListening] = useState(false);
  const [recognition, setRecognition] = useState<SpeechRecognition | null>(
    null,
  );
  const recognitionRef = useRef<SpeechRecognition | null>(null);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      ("SpeechRecognition" in window || "webkitSpeechRecognition" in window)
    ) {
      const SpeechRecognition =
        window.SpeechRecognition || window.webkitSpeechRecognition;
      const speechRecognition = new SpeechRecognition();

      speechRecognition.continuous = true;
      speechRecognition.interimResults = true;
      speechRecognition.lang = navigator.language || "en-US";

      speechRecognition.onstart = () => {
        setIsListening(true);
      };

      speechRecognition.onend = () => {
        setIsListening(false);
      };

      speechRecognition.onresult = (event) => {
        let finalTranscript = "";

        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i];
          if (result.isFinal) {
            finalTranscript += result[0]?.transcript ?? "";
          }
        }

        if (finalTranscript && textareaRef?.current) {
          const textarea = textareaRef.current;
          const currentValue = textarea.value;
          const newValue =
            currentValue + (currentValue ? " " : "") + finalTranscript;

          textarea.value = newValue;
          textarea.dispatchEvent(new Event("input", { bubbles: true }));
          onTranscriptionChange?.(newValue);
        }
      };

      speechRecognition.onerror = (event) => {
        console.error("Speech recognition error:", event.error);
        setIsListening(false);
      };

      recognitionRef.current = speechRecognition;
      setRecognition(speechRecognition);
    }

    return () => {
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
    };
  }, [textareaRef, onTranscriptionChange]);

  const toggleListening = useCallback(() => {
    if (!recognition) {
      return;
    }

    if (isListening) {
      recognition.stop();
    } else {
      recognition.start();
    }
  }, [recognition, isListening]);

  return (
    <PromptInputButton
      className={cn(
        "relative transition-all duration-200",
        isListening && "animate-pulse bg-accent text-accent-foreground",
        className,
      )}
      disabled={!recognition}
      onClick={toggleListening}
      {...props}
    >
      <MicIcon className="size-4" />
    </PromptInputButton>
  );
};

export type PromptInputSelectProps = ComponentProps<typeof Select>;

export const PromptInputSelect = (props: PromptInputSelectProps) => (
  <Select {...props} />
);

export type PromptInputSelectTriggerProps = ComponentProps<
  typeof SelectTrigger
>;

export const PromptInputSelectTrigger = ({
  className,
  ...props
}: PromptInputSelectTriggerProps) => (
  <SelectTrigger
    className={cn(
      "border-none bg-transparent font-medium text-muted-foreground shadow-none transition-colors",
      "hover:bg-accent hover:text-foreground aria-expanded:bg-accent aria-expanded:text-foreground",
      className,
    )}
    {...props}
  />
);

export type PromptInputSelectContentProps = ComponentProps<
  typeof SelectContent
>;

export const PromptInputSelectContent = ({
  className,
  ...props
}: PromptInputSelectContentProps) => (
  <SelectContent className={cn(className)} {...props} />
);

export type PromptInputSelectItemProps = ComponentProps<typeof SelectItem>;

export const PromptInputSelectItem = ({
  className,
  ...props
}: PromptInputSelectItemProps) => (
  <SelectItem className={cn(className)} {...props} />
);

export type PromptInputSelectValueProps = ComponentProps<typeof SelectValue>;

export const PromptInputSelectValue = ({
  className,
  ...props
}: PromptInputSelectValueProps) => (
  <SelectValue className={cn(className)} {...props} />
);

export type PromptInputHoverCardProps = ComponentProps<typeof HoverCard>;

export const PromptInputHoverCard = ({
  openDelay = 0,
  closeDelay = 0,
  ...props
}: PromptInputHoverCardProps) => (
  <HoverCard closeDelay={closeDelay} openDelay={openDelay} {...props} />
);

export type PromptInputHoverCardTriggerProps = ComponentProps<
  typeof HoverCardTrigger
>;

export const PromptInputHoverCardTrigger = (
  props: PromptInputHoverCardTriggerProps,
) => <HoverCardTrigger {...props} />;

export type PromptInputHoverCardContentProps = ComponentProps<
  typeof HoverCardContent
>;

export const PromptInputHoverCardContent = ({
  align = "start",
  ...props
}: PromptInputHoverCardContentProps) => (
  <HoverCardContent align={align} {...props} />
);

export type PromptInputTabsListProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTabsList = ({
  className,
  ...props
}: PromptInputTabsListProps) => <div className={cn(className)} {...props} />;

export type PromptInputTabProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTab = ({
  className,
  ...props
}: PromptInputTabProps) => <div className={cn(className)} {...props} />;

export type PromptInputTabLabelProps = HTMLAttributes<HTMLHeadingElement>;

export const PromptInputTabLabel = ({
  className,
  ...props
}: PromptInputTabLabelProps) => (
  <h3
    className={cn(
      "mb-2 px-3 font-medium text-muted-foreground text-xs",
      className,
    )}
    {...props}
  />
);

export type PromptInputTabBodyProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTabBody = ({
  className,
  ...props
}: PromptInputTabBodyProps) => (
  <div className={cn("space-y-1", className)} {...props} />
);

export type PromptInputTabItemProps = HTMLAttributes<HTMLDivElement>;

export const PromptInputTabItem = ({
  className,
  ...props
}: PromptInputTabItemProps) => (
  <div
    className={cn(
      "flex items-center gap-2 px-3 py-2 text-xs hover:bg-accent",
      className,
    )}
    {...props}
  />
);

export type PromptInputCommandProps = ComponentProps<typeof Command>;

export const PromptInputCommand = ({
  className,
  ...props
}: PromptInputCommandProps) => <Command className={cn(className)} {...props} />;

export type PromptInputCommandInputProps = ComponentProps<typeof CommandInput>;

export const PromptInputCommandInput = ({
  className,
  ...props
}: PromptInputCommandInputProps) => (
  <CommandInput className={cn(className)} {...props} />
);

export type PromptInputCommandListProps = ComponentProps<typeof CommandList>;

export const PromptInputCommandList = ({
  className,
  ...props
}: PromptInputCommandListProps) => (
  <CommandList className={cn(className)} {...props} />
);

export type PromptInputCommandEmptyProps = ComponentProps<typeof CommandEmpty>;

export const PromptInputCommandEmpty = ({
  className,
  ...props
}: PromptInputCommandEmptyProps) => (
  <CommandEmpty className={cn(className)} {...props} />
);

export type PromptInputCommandGroupProps = ComponentProps<typeof CommandGroup>;

export const PromptInputCommandGroup = ({
  className,
  ...props
}: PromptInputCommandGroupProps) => (
  <CommandGroup className={cn(className)} {...props} />
);

export type PromptInputCommandItemProps = ComponentProps<typeof CommandItem>;

export const PromptInputCommandItem = ({
  className,
  ...props
}: PromptInputCommandItemProps) => (
  <CommandItem className={cn(className)} {...props} />
);

export type PromptInputCommandSeparatorProps = ComponentProps<
  typeof CommandSeparator
>;

export const PromptInputCommandSeparator = ({
  className,
  ...props
}: PromptInputCommandSeparatorProps) => (
  <CommandSeparator className={cn(className)} {...props} />
);
