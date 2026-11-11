import {
  useState,
  useCallback,
  useEffect,
  useRef,
} from "react";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";
import {
  getAdminBranding,
  updateBranding,
  resetBranding,
  type BrandingConfig,
} from "@/lib/api/apis/BrandingApi";
import { BRANDING_DEFAULTS } from "@/hooks/useBranding";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Palette, Upload, RotateCcw, Loader2, Save, Image } from "lucide-react";
import { toast } from "sonner";

type FormState = {
  brand_name: string;
  version: string;
  page_title: string;
  logo_url: string;
  logo: string | null;
  favicon: string | null;
};

const EMPTY_FORM: FormState = {
  brand_name: "",
  version: "",
  page_title: "",
  logo_url: "",
  logo: null,
  favicon: null,
};

function configToForm(config: BrandingConfig): FormState {
  return {
    brand_name: config.brand_name ?? "",
    version: config.version ?? "",
    page_title: config.page_title ?? "",
    logo_url: config.logo_url ?? "",
    logo: config.logo,
    favicon: config.favicon,
  };
}

function broadcastBrandingUpdate() {
  window.dispatchEvent(new Event("kimo:branding-update"));
  const channel = new BroadcastChannel("kimo:branding");
  channel.postMessage("updated");
  channel.close();
}

function handleFileUpload(
  file: File,
  options: { maxSizeKB: number; accept: string[] },
  onResult: (dataUrl: string) => void,
) {
  if (!options.accept.includes(file.type)) {
    toast.error(i18n.t("admin:branding.unsupportedFormat", { type: file.type }));
    return;
  }
  if (file.size > options.maxSizeKB * 1024) {
    toast.error(i18n.t("admin:branding.fileTooLarge", { maxKB: options.maxSizeKB }));
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    onResult(reader.result as string);
  };
  reader.readAsDataURL(file);
}

export function AdminBrandingPanel() {
  const { t } = useTranslation("admin");
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [savedForm, setSavedForm] = useState<FormState>(EMPTY_FORM);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [showResetDialog, setShowResetDialog] = useState(false);
  const [isResetting, setIsResetting] = useState(false);

  const logoInputRef = useRef<HTMLInputElement>(null);
  const faviconInputRef = useRef<HTMLInputElement>(null);

  const loadBranding = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await getAdminBranding();
      const formData = configToForm(data);
      setForm(formData);
      setSavedForm(formData);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("branding.loadFailed"));
    } finally {
      setIsLoading(false);
    }
  }, [t]);

  useEffect(() => {
    loadBranding();
  }, [loadBranding]);

  const hasChanges =
    form.brand_name !== savedForm.brand_name ||
    form.version !== savedForm.version ||
    form.page_title !== savedForm.page_title ||
    form.logo_url !== savedForm.logo_url ||
    form.logo !== savedForm.logo ||
    form.favicon !== savedForm.favicon;

  const handleSave = useCallback(async () => {
    setIsSaving(true);
    try {
      const payload: Partial<BrandingConfig> = {
        brand_name: form.brand_name || null,
        version: form.version || null,
        page_title: form.page_title || null,
        logo_url: form.logo_url || null,
        logo: form.logo,
        favicon: form.favicon,
      };
      const updated = await updateBranding(payload);
      const formData = configToForm(updated);
      setForm(formData);
      setSavedForm(formData);
      toast.success(t("branding.saved"));
      broadcastBrandingUpdate();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("branding.saveFailed"));
    } finally {
      setIsSaving(false);
    }
  }, [form, t]);

  const handleReset = useCallback(async () => {
    setIsResetting(true);
    try {
      await resetBranding();
      setForm(EMPTY_FORM);
      setSavedForm(EMPTY_FORM);
      toast.success(t("branding.resetDone"));
      broadcastBrandingUpdate();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t("branding.resetFailed"));
    } finally {
      setIsResetting(false);
      setShowResetDialog(false);
    }
  }, [t]);

  const updateField = useCallback((field: keyof FormState, value: string | null) => {
    setForm((prev) => ({ ...prev, [field]: value }));
  }, []);

  const previewLogo = form.logo ?? BRANDING_DEFAULTS.logo;
  const previewFavicon = form.favicon ?? BRANDING_DEFAULTS.favicon;
  const previewName = form.brand_name || BRANDING_DEFAULTS.brand_name;
  const previewVersion = form.version || "0.0.0";

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 className="animate-spin size-4" />
        {t("branding.loading")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Live Preview */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
            <Palette className="size-4" />
            {t("branding.livePreview")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3 rounded-lg border bg-muted/20 p-4">
            <img
              src={previewLogo}
              alt={previewName}
              width={28}
              height={28}
              className="size-7 rounded"
            />
            <span className="text-lg font-semibold text-foreground">
              {previewName}
            </span>
            <span className="text-sm text-muted-foreground font-medium">
              v{previewVersion}
            </span>
          </div>
        </CardContent>
      </Card>

      {/* Logo Upload */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
            <Image className="size-4" />
            {t("branding.logo")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4">
            <div className="flex size-16 items-center justify-center rounded-lg border bg-muted/20">
              <img
                src={previewLogo}
                alt={t("branding.logoPreviewAlt")}
                width={64}
                height={64}
                className="size-16 rounded-lg object-contain"
              />
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2"
                  onClick={() => logoInputRef.current?.click()}
                >
                  <Upload className="size-3.5" />
                  {t("branding.uploadLogo")}
                </Button>
                {form.logo && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-2 text-muted-foreground"
                    onClick={() => updateField("logo", null)}
                  >
                    <RotateCcw className="size-3.5" />
                    {t("branding.resetToDefault")}
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {t("branding.logoHint")}
              </p>
            </div>
            <input
              ref={logoInputRef}
              type="file"
              accept="image/png,image/svg+xml,image/jpeg"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  handleFileUpload(
                    file,
                    { maxSizeKB: 512, accept: ["image/png", "image/svg+xml", "image/jpeg"] },
                    (dataUrl) => updateField("logo", dataUrl),
                  );
                }
                e.target.value = "";
              }}
            />
          </div>
        </CardContent>
      </Card>

      {/* Favicon Upload */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground flex items-center gap-2">
            <Image className="size-4" />
            {t("branding.favicon")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4">
            <div className="flex size-8 items-center justify-center rounded border bg-muted/20">
              <img
                src={previewFavicon}
                alt={t("branding.faviconPreviewAlt")}
                width={32}
                height={32}
                className="size-8 rounded object-contain"
              />
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2"
                  onClick={() => faviconInputRef.current?.click()}
                >
                  <Upload className="size-3.5" />
                  {t("branding.uploadFavicon")}
                </Button>
                {form.favicon && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-2 text-muted-foreground"
                    onClick={() => updateField("favicon", null)}
                  >
                    <RotateCcw className="size-3.5" />
                    {t("branding.resetToDefault")}
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-foreground">
                {t("branding.faviconHint")}
              </p>
            </div>
            <input
              ref={faviconInputRef}
              type="file"
              accept="image/x-icon,image/png,image/svg+xml"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  handleFileUpload(
                    file,
                    { maxSizeKB: 256, accept: ["image/x-icon", "image/png", "image/svg+xml"] },
                    (dataUrl) => updateField("favicon", dataUrl),
                  );
                }
                e.target.value = "";
              }}
            />
          </div>
        </CardContent>
      </Card>

      {/* Text Fields */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {t("branding.textSettings")}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="brand-name" className="text-sm font-medium text-foreground">
                {t("branding.brandName")}
              </label>
              <span className="text-xs text-muted-foreground">
                {form.brand_name.length}/30
              </span>
            </div>
            <Input
              id="brand-name"
              type="text"
              placeholder={BRANDING_DEFAULTS.brand_name}
              value={form.brand_name}
              maxLength={30}
              onChange={(e) => updateField("brand_name", e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="brand-version" className="text-sm font-medium text-foreground">
                {t("branding.version")}
              </label>
              <span className="text-xs text-muted-foreground">
                {form.version.length}/20
              </span>
            </div>
            <Input
              id="brand-version"
              type="text"
              placeholder={t("branding.versionPlaceholder")}
              value={form.version}
              maxLength={20}
              onChange={(e) => updateField("version", e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="page-title" className="text-sm font-medium text-foreground">
                {t("branding.pageTitle")}
              </label>
              <span className="text-xs text-muted-foreground">
                {form.page_title.length}/60
              </span>
            </div>
            <Input
              id="page-title"
              type="text"
              placeholder={BRANDING_DEFAULTS.page_title}
              value={form.page_title}
              maxLength={60}
              onChange={(e) => updateField("page_title", e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="logo-url" className="text-sm font-medium text-foreground">
              {t("branding.logoUrl")}
            </label>
            <Input
              id="logo-url"
              type="url"
              placeholder={BRANDING_DEFAULTS.logo_url}
              value={form.logo_url}
              onChange={(e) => updateField("logo_url", e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t("branding.logoUrlHint")}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Action Buttons */}
      <div className="flex items-center gap-3">
        <Button
          onClick={handleSave}
          disabled={!hasChanges || isSaving}
          className="gap-2"
        >
          {isSaving ? (
            <><Loader2 className="animate-spin size-4" />{t("actions.saving")}</>
          ) : (
            <><Save className="size-4" />{t("branding.saveChanges")}</>
          )}
        </Button>
        <Button
          variant="outline"
          onClick={() => setShowResetDialog(true)}
          className="gap-2"
        >
          <RotateCcw className="size-4" />
          {t("branding.resetToDefaults")}
        </Button>
      </div>

      {/* Reset Confirmation Dialog */}
      <AlertDialog open={showResetDialog} onOpenChange={setShowResetDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("branding.resetTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("branding.resetDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isResetting}>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={isResetting}
              onClick={handleReset}
            >
              {isResetting ? (
                <><Loader2 className="animate-spin size-4" />{t("actions.resetting")}</>
              ) : (
                t("branding.resetToDefaults")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
