import { forwardRef, useImperativeHandle, useRef } from "react";
import { AlertTriangle } from "lucide-react";

export interface ConfirmDialogHandle {
  open: () => void;
  close: () => void;
}

export const ConfirmDialog = forwardRef<ConfirmDialogHandle, {
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
}>(({ title, body, confirmLabel, onConfirm }, ref) => {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useImperativeHandle(ref, () => ({
    open: () => dialogRef.current?.showModal(),
    close: () => dialogRef.current?.close()
  }));
  return (
    <dialog ref={dialogRef} className="w-full max-w-md border border-line bg-paper p-0 text-ink shadow-xl">
      <form method="dialog" className="space-y-4 p-5">
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 text-amber" aria-hidden />
          <div>
            <h2 className="text-base font-semibold">{title}</h2>
            <p className="mt-2 text-sm text-stone-700">{body}</p>
          </div>
        </div>
        <div className="flex justify-end gap-2">
          <button className="focus-ring border border-line bg-white px-3 py-1.5 text-sm" value="cancel">Cancel</button>
          <button
            className="focus-ring border border-brick bg-brick px-3 py-1.5 text-sm text-white"
            value="confirm"
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
});

ConfirmDialog.displayName = "ConfirmDialog";
