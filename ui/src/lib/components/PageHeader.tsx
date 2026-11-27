import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";

export interface Breadcrumb {
  label: string;
  to?: string;
}

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  /** Small all-caps label above the title. */
  eyebrow?: string;
  icon?: IconName;
  breadcrumbs?: Breadcrumb[];
  actions?: ReactNode;
  /** Secondary metadata row under the title (badges, timestamps…). */
  meta?: ReactNode;
  children?: ReactNode;
  className?: string;
}

/**
 * Consistent page title block. Every page should start with a `PageHeader`;
 * the `h1` inside it is what the shell's document title and tests look for.
 */
export function PageHeader({
  title,
  description,
  eyebrow,
  icon,
  breadcrumbs,
  actions,
  meta,
  children,
  className,
}: PageHeaderProps) {
  return (
    <header className={cx("ds-page-header", className)}>
      {breadcrumbs && breadcrumbs.length > 0 ? (
        <nav aria-label="Breadcrumb" className="ds-breadcrumbs">
          <ol>
            {breadcrumbs.map((crumb, index) => {
              const last = index === breadcrumbs.length - 1;
              return (
                <li key={`${crumb.label}-${index}`}>
                  {crumb.to && !last ? (
                    <Link to={crumb.to} className="ds-breadcrumbs__link">
                      {crumb.label}
                    </Link>
                  ) : (
                    <span aria-current={last ? "page" : undefined} className="ds-breadcrumbs__current">
                      {crumb.label}
                    </span>
                  )}
                  {last ? null : (
                    <Icon name="chevron-right" size={13} className="ds-breadcrumbs__sep" aria-hidden="true" />
                  )}
                </li>
              );
            })}
          </ol>
        </nav>
      ) : null}
      <div className="ds-page-header__main">
        <div className="ds-page-header__heading">
          {eyebrow ? <p className="ds-page-header__eyebrow">{eyebrow}</p> : null}
          <div className="ds-page-header__title-row">
            {icon ? (
              <span className="ds-page-header__icon" aria-hidden="true">
                <Icon name={icon} size={22} />
              </span>
            ) : null}
            <h1 className="ds-page-header__title">{title}</h1>
          </div>
          {description ? <p className="ds-page-header__description">{description}</p> : null}
          {meta ? <div className="ds-page-header__meta">{meta}</div> : null}
        </div>
        {actions ? <div className="ds-page-header__actions">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}
