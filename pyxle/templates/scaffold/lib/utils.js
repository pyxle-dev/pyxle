import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

// The `cn` helper shadcn/ui components use to merge Tailwind class names,
// resolving conflicts (later utilities win) so component variants compose
// cleanly. Imported as `@/lib/utils`.
export function cn(...inputs) {
    return twMerge(clsx(inputs));
}
