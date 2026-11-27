import { Icon } from "@/lib/components";

import { DEPLOY_STEPS, type DeployStepId } from "./deploy-model";

export interface DeployStepperProps {
  current: DeployStepId;
  /** Called when an already-reached step is clicked (back navigation). */
  onSelect: (id: DeployStepId) => void;
}

/**
 * Accessible wizard stepper. Steps already visited are clickable buttons;
 * future steps are disabled. Native buttons give full keyboard support.
 */
export function DeployStepper({ current, onSelect }: DeployStepperProps) {
  const currentIndex = DEPLOY_STEPS.findIndex((step) => step.id === current);
  const currentStep = DEPLOY_STEPS[currentIndex] ?? DEPLOY_STEPS[0]!;

  return (
    <nav aria-label="Deploy steps">
      <p className="visually-hidden" aria-live="polite">
        Step {currentIndex + 1} of {DEPLOY_STEPS.length}: {currentStep.label}
      </p>
      <ol
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-2)",
          listStyle: "none",
          padding: 0,
          margin: 0,
        }}
      >
        {DEPLOY_STEPS.map((step, index) => {
          const isCurrent = index === currentIndex;
          const isComplete = index < currentIndex;
          const disabled = index > currentIndex;
          return (
            <li key={step.id} style={{ flex: "1 1 180px", minWidth: 0 }}>
              <button
                type="button"
                disabled={disabled}
                aria-current={isCurrent ? "step" : undefined}
                onClick={() => onSelect(step.id)}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: "var(--space-3)",
                  width: "100%",
                  height: "100%",
                  textAlign: "left",
                  padding: "var(--space-3)",
                  border: `1px solid ${isCurrent ? "var(--accent)" : "var(--border)"}`,
                  borderRadius: "var(--radius-md)",
                  background: isCurrent
                    ? "var(--accent-soft)"
                    : isComplete
                      ? "var(--surface-sunken)"
                      : "transparent",
                  color: disabled ? "var(--text-subtle)" : "var(--text)",
                  cursor: disabled ? "not-allowed" : "pointer",
                  opacity: disabled ? 0.65 : 1,
                }}
              >
                <span
                  aria-hidden="true"
                  style={{
                    display: "grid",
                    placeItems: "center",
                    width: 26,
                    height: 26,
                    flex: "none",
                    borderRadius: "50%",
                    border: `1px solid ${isCurrent || isComplete ? "var(--accent)" : "var(--border)"}`,
                    background: isComplete ? "var(--accent)" : "var(--surface-sunken)",
                    color: isComplete ? "var(--accent-contrast)" : "var(--accent)",
                    fontSize: "var(--fs-xs)",
                    fontWeight: 700,
                  }}
                >
                  {isComplete ? <Icon name="check" size={14} /> : index + 1}
                </span>
                <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                  <span style={{ fontWeight: 620, fontSize: "var(--fs-sm)" }}>{step.label}</span>
                  <span
                    className="muted"
                    style={{ fontSize: "var(--fs-xs)", whiteSpace: "normal", lineHeight: 1.35 }}
                  >
                    {step.description}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
