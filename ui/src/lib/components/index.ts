/**
 * AVD Fleet design system.
 *
 * Import components from `@/lib/components` — never deep-import a component
 * file, so the barrel stays the stable surface for page agents.
 */
export { Icon, type IconName, type IconProps } from "./Icon";
export {
  Spinner,
  Skeleton,
  EmptyState,
  ProgressBar,
  InlineNotice,
  type SpinnerProps,
  type SkeletonProps,
  type EmptyStateProps,
  type ProgressBarProps,
  type InlineNoticeProps,
  type NoticeTone,
} from "./Feedback";
export {
  Button,
  ButtonLink,
  IconButton,
  type ButtonProps,
  type ButtonLinkProps,
  type IconButtonProps,
  type ButtonVariant,
  type ButtonSize,
} from "./Button";
export { Card, StatCard, type CardProps, type StatCardProps } from "./Card";
export {
  Badge,
  StatusPill,
  statusTone,
  type BadgeProps,
  type BadgeTone,
  type BadgeSize,
  type StatusPillProps,
} from "./Badge";
export { PageHeader, type PageHeaderProps, type Breadcrumb } from "./PageHeader";
export { Table, type TableProps, type Column } from "./Table";
export {
  Field,
  Input,
  Select,
  Textarea,
  Checkbox,
  SearchBox,
  type FieldProps,
  type InputProps,
  type SelectProps,
  type SelectOption,
  type TextareaProps,
  type CheckboxProps,
  type SearchBoxProps,
} from "./Form";
export { Tabs, type TabsProps, type TabItem } from "./Tabs";
export { Modal, ConfirmDialog, type ModalProps, type ConfirmDialogProps } from "./Modal";
export { ToastProvider, useToast, type ToastApi, type ToastOptions, type ToastTone } from "./Toast";
export {
  CodeBlock,
  LogViewer,
  type CodeBlockProps,
  type LogViewerProps,
  type LogLine,
  type LogStream,
} from "./CodeBlock";
