import { useTheme } from "next-themes"
import { Toaster as Sonner, toast } from "sonner"

// Round 4 (2026-07-06) — error prompts, field-tested rules:
//   * NEVER auto-dismiss: the super reads it and closes it (X) when done.
//     Success/info toasts keep their normal timed dismissal.
//   * Clearly distinct from the green action buttons (richColors renders
//     errors on a red surface) and positioned TOP-center on phones so they
//     can't hide behind the sticky footer buttons.
//   * Minimal: same compact toast, just persistent + closable.
// Patching toast.error on the sonner module instance covers every caller —
// including files importing { toast } from "sonner" directly.
const _error = toast.error.bind(toast);
toast.error = (message, opts = {}) => _error(message, {
  duration: Infinity,
  closeButton: true,
  ...opts,
});

const IS_PHONE = typeof window !== "undefined"
  && typeof window.matchMedia === "function"
  && window.matchMedia("(max-width: 767.98px)").matches;

const Toaster = ({
  ...props
}) => {
  const { theme = "system" } = useTheme()

  return (
    <Sonner
      theme={theme}
      className="toaster group"
      richColors
      closeButton
      position={IS_PHONE ? "top-center" : "bottom-right"}
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          actionButton:
            "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
        },
      }}
      {...props} />
  );
}

export { Toaster, toast }
