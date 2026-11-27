import type { ReactNode } from "react";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";
import { Skeleton } from "./Feedback";

export interface CardProps extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  icon?: IconName;
  /** Remove inner padding (e.g. for full-bleed tables). */
  flush?: boolean;
  tone?: "default" | "muted" | "accent";
}

export function Card({
  title,
  description,
  actions,
  footer,
  icon,
  flush = false,
  tone = "default",
  className,
  children,
  ...rest
}: CardProps) {
  const hasHeader = Boolean(title || description || actions);
  return (
    <section
      className={cx("ds-card", `ds-card--${tone}`, flush && "ds-card--flush", className)}
      {...rest}
    >
      {hasHeader ? (
        <header className="ds-card__header">
          <div className="ds-card__heading">
            <div className="ds-card__title-row">
              {icon ? <Icon name={icon} size={17} className="ds-card__icon" /> : null}
              {typeof title === "string" ? <h2 className="ds-card__title">{title}</h2> : title}
            </div>
            {description ? <p className="ds-card__description">{description}</p> : null}
          </div>
          {actions ? <div className="ds-card__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="ds-card__body">{children}</div>
      {footer ? <footer className="ds-card__footer">{footer}</footer> : null}
    </section>
  );
}

export interface StatCardProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: IconName;
  tone?: "default" | "success" | "warning" | "danger" | "accent";
  loading?: boolean;
  footer?: ReactNode;
  className?: string;
}

export function StatCard({
  label,
  value,
  hint,
  icon,
  tone = "default",
  loading = false,
  footer,
  className,
}: StatCardProps) {
  return (
    <div className={cx("ds-stat", `ds-stat--${tone}`, className)}>
      <div className="ds-stat__top">
        <span className="ds-stat__label">{label}</span>
        {icon ? (
          <span className="ds-stat__icon" aria-hidden="true">
            <Icon name={icon} size={17} />
          </span>
        ) : null}
      </div>
      <div className="ds-stat__value">
        {loading ? <Skeleton width="58%" height={30} /> : value}
      </div>
      {hint ? <div className="ds-stat__hint">{hint}</div> : null}
      {footer ? <div className="ds-stat__footer">{footer}</div> : null}
    </div>
  );
}
