/** The AVD Fleet brand mark (two device outlines). Decorative by default. */
export function LogoMark({ size = 26 }: { size?: number }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} fill="none" aria-hidden="true" focusable="false">
      <rect x="4" y="3" width="9" height="26" rx="3" stroke="currentColor" strokeWidth="2.2" />
      <rect
        x="19"
        y="3"
        width="9"
        height="26"
        rx="3"
        stroke="currentColor"
        strokeWidth="2.2"
        opacity="0.6"
      />
      <circle cx="8.5" cy="25" r="1.5" fill="currentColor" />
      <circle cx="23.5" cy="25" r="1.5" fill="currentColor" opacity="0.6" />
    </svg>
  );
}
