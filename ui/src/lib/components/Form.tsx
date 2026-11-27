import {
  cloneElement,
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { cx } from "../utils";
import { Icon, type IconName } from "./Icon";

/* -------------------------------------------------------------------------- */
/* Field                                                                       */
/* -------------------------------------------------------------------------- */

export interface FieldProps {
  label: ReactNode;
  htmlFor?: string;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** A single form control (Input, Select, Textarea, Checkbox…). */
  children: ReactNode;
  className?: string;
  id?: string;
}

/**
 * Label + hint + error wrapper. Wires `id`, `aria-describedby` and
 * `aria-invalid` onto the child control automatically.
 */
export function Field({ label, htmlFor, hint, error, required, children, className, id }: FieldProps) {
  const generatedId = useId();
  const controlId = htmlFor ?? id ?? `field-${generatedId}`;
  const hintId = hint ? `${controlId}-hint` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  let control = children;
  if (children && typeof children === "object" && "props" in (children as object)) {
    const element = children as ReactElement<Record<string, unknown>>;
    const existing = element.props;
    const existingDescribedBy =
      typeof existing["aria-describedby"] === "string" ? existing["aria-describedby"] : undefined;
    control = cloneElement(element, {
      id: (existing.id as string | undefined) ?? controlId,
      "aria-describedby": [existingDescribedBy, describedBy].filter(Boolean).join(" ") || undefined,
      "aria-invalid": error ? true : existing["aria-invalid"],
      invalid: (existing.invalid as boolean | undefined) ?? Boolean(error),
      required: (existing.required as boolean | undefined) ?? required,
    });
  }

  return (
    <div className={cx("ds-field", error && "ds-field--invalid", className)}>
      <label className="ds-field__label" htmlFor={controlId}>
        {label}
        {required ? (
          <span className="ds-field__required" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>
      {control}
      {hint ? (
        <p className="ds-field__hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="ds-field__error" id={errorId} role="alert">
          <Icon name="alert-circle" size={13} aria-hidden="true" />
          {error}
        </p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Text inputs                                                                 */
/* -------------------------------------------------------------------------- */

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  leadingIcon?: IconName;
  trailing?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid = false, leadingIcon, trailing, className, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      className={cx("ds-input", invalid && "is-invalid", leadingIcon && "ds-input--with-icon", className)}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
  if (!leadingIcon && !trailing) return input;
  return (
    <span className={cx("ds-input-wrap", invalid && "is-invalid")}>
      {leadingIcon ? (
        <span className="ds-input-wrap__icon" aria-hidden="true">
          <Icon name={leadingIcon} size={16} />
        </span>
      ) : null}
      {input}
      {trailing ? <span className="ds-input-wrap__trailing">{trailing}</span> : null}
    </span>
  );
});

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean;
  options?: SelectOption[];
  placeholder?: string;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid = false, options, placeholder, className, children, ...rest },
  ref,
) {
  return (
    <span className="ds-select-wrap">
      <select
        ref={ref}
        className={cx("ds-select", invalid && "is-invalid", className)}
        aria-invalid={invalid || undefined}
        {...rest}
      >
        {placeholder ? (
          <option value="" disabled>
            {placeholder}
          </option>
        ) : null}
        {options?.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
        {children}
      </select>
      <Icon name="chevron-down" size={15} className="ds-select-wrap__icon" aria-hidden="true" />
    </span>
  );
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid = false, className, rows = 4, ...rest },
  ref,
) {
  return (
    <textarea
      ref={ref}
      rows={rows}
      className={cx("ds-textarea", invalid && "is-invalid", className)}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
});

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
  hint?: ReactNode;
}

export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { label, hint, className, id, ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? `checkbox-${generatedId}`;
  const hintId = hint ? `${inputId}-hint` : undefined;
  return (
    <div className={cx("ds-checkbox", className)}>
      <input
        ref={ref}
        id={inputId}
        type="checkbox"
        className="ds-checkbox__input"
        aria-describedby={hintId}
        {...rest}
      />
      <div className="ds-checkbox__body">
        <label className="ds-checkbox__label" htmlFor={inputId}>
          {label}
        </label>
        {hint ? (
          <p className="ds-checkbox__hint" id={hintId}>
            {hint}
          </p>
        ) : null}
      </div>
    </div>
  );
});

/* -------------------------------------------------------------------------- */
/* SearchBox                                                                   */
/* -------------------------------------------------------------------------- */

export interface SearchBoxProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  label?: string;
  className?: string;
}

export function SearchBox({
  value,
  onChange,
  placeholder = "Search…",
  label = "Search",
  className,
}: SearchBoxProps) {
  return (
    <div className={cx("ds-search", className)}>
      <Icon name="search" size={16} className="ds-search__icon" aria-hidden="true" />
      <input
        type="search"
        className="ds-search__input"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        aria-label={label}
      />
    </div>
  );
}
