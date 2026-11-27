import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link, type LinkProps } from "react-router-dom";

import { cx } from "../utils";
import { Spinner } from "./Feedback";
import { Icon, type IconName } from "./Icon";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: IconName;
  iconRight?: IconName;
  loading?: boolean;
  fullWidth?: boolean;
  type?: "button" | "submit" | "reset";
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    icon,
    iconRight,
    loading = false,
    fullWidth = false,
    type = "button",
    className,
    children,
    disabled,
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx(
        "ds-btn",
        `ds-btn--${variant}`,
        `ds-btn--${size}`,
        fullWidth && "ds-btn--block",
        loading && "is-loading",
        className,
      )}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Spinner size="sm" label="Working" /> : icon ? <Icon name={icon} size={size === "sm" ? 15 : 17} /> : null}
      {children ? <span className="ds-btn__label">{children}</span> : null}
      {iconRight && !loading ? (
        <Icon name={iconRight} size={size === "sm" ? 15 : 17} className="ds-btn__icon-right" />
      ) : null}
    </button>
  );
});

export interface ButtonLinkProps extends Omit<LinkProps, "className"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: IconName;
  iconRight?: IconName;
  fullWidth?: boolean;
  className?: string;
  children?: ReactNode;
}

/** A router `<Link>` styled as a button — correct semantics for navigation. */
export function ButtonLink({
  variant = "secondary",
  size = "md",
  icon,
  iconRight,
  fullWidth = false,
  className,
  children,
  ...rest
}: ButtonLinkProps) {
  return (
    <Link
      className={cx(
        "ds-btn",
        `ds-btn--${variant}`,
        `ds-btn--${size}`,
        fullWidth && "ds-btn--block",
        className,
      )}
      {...rest}
    >
      {icon ? <Icon name={icon} size={size === "sm" ? 15 : 17} /> : null}
      {children ? <span className="ds-btn__label">{children}</span> : null}
      {iconRight ? (
        <Icon name={iconRight} size={size === "sm" ? 15 : 17} className="ds-btn__icon-right" />
      ) : null}
    </Link>
  );
}

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type" | "aria-label"> {
  /** Required accessible name (rendered as title too, unless `title` given). */
  label: string;
  icon: IconName;
  variant?: ButtonVariant | "light";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  type?: "button" | "submit";
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, variant = "ghost", size = "md", loading = false, className, type = "button", title, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("ds-icon-btn", `ds-icon-btn--${variant}`, `ds-icon-btn--${size}`, className)}
      aria-label={label}
      title={title ?? label}
      aria-busy={loading || undefined}
      disabled={rest.disabled || loading}
      {...rest}
    >
      {loading ? <Spinner size="sm" label={label} /> : <Icon name={icon} size={size === "lg" ? 20 : 18} />}
    </button>
  );
});
