import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableInputProps,
} from '../../components/WorkbenchLayout';
import type { ConfigFieldSchema, PluginItem } from '../../data/mock';

export interface PluginConfigPanelProps {
  plugin: PluginItem;
  configValues: Record<string, string | boolean | number>;
  revealedSecrets: Record<string, boolean>;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onUpdateField: (key: string, value: string | boolean | number) => void;
  onToggleSecretReveal: (key: string) => void;
  onSave: () => void;
  onReset: () => void;
}

export const PluginConfigPanel: React.FC<PluginConfigPanelProps> = ({
  plugin,
  configValues,
  revealedSecrets,
  activeFocusId,
  onFocus,
  onUpdateField,
  onToggleSecretReveal,
  onSave,
  onReset,
}) => {
  const renderField = (field: ConfigFieldSchema) => {
    const fieldFocusId = `store:field:${plugin.id}:${field.key}`;
    const maskFocusId = `store:field-mask:${plugin.id}:${field.key}`;
    const rawValue = configValues[field.key] ?? field.default;
    const secretKey = `${plugin.id}:${field.key}`;
    const isRevealed = Boolean(revealedSecrets[secretKey]);

    return (
      <div
        key={field.key}
        className="flex flex-col gap-1.5 rounded-lg border p-3"
        style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="flex items-center justify-between gap-2">
          <label htmlFor={fieldFocusId} className="text-xs font-semibold">
            {field.label}
            {field.required ? (
              <span style={{ color: 'var(--status-err-fg, #B6263A)' }}> *</span>
            ) : null}
          </label>
          <span
            className="rounded px-1.5 py-0.5 font-mono text-[10px]"
            style={{
              backgroundColor:
                field.type === 'secret'
                  ? 'var(--status-warn-bg, #FCF2DF)'
                  : 'var(--bg-soft, #F1EFEC)',
              color:
                field.type === 'secret'
                  ? 'var(--status-warn-fg, #AC5F0C)'
                  : 'var(--text-muted, #66625D)',
            }}
          >
            {field.type === 'secret' ? '0600 SecretVault' : field.type}
          </span>
        </div>

        {field.type === 'bool' ? (
          <FocusButton
            id={fieldFocusId}
            focusId={fieldFocusId}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onUpdateField(field.key, !Boolean(rawValue))}
            selected={Boolean(rawValue)}
            tone="ok"
            surface="surface"
            role="switch"
            ariaChecked={Boolean(rawValue)}
            className="flex items-center justify-between px-3 py-1.5 text-xs font-medium"
          >
            <span>{Boolean(rawValue) ? '已开启 (true)' : '已关闭 (false)'}</span>
            <span className="font-mono text-[10px]">{field.key}</span>
          </FocusButton>
        ) : field.type === 'select' ? (
          <select
            id={fieldFocusId}
            value={String(rawValue)}
            onChange={(e) => onUpdateField(field.key, e.target.value)}
            data-focused={activeFocusId === fieldFocusId ? 'true' : undefined}
            {...focusableInputProps(fieldFocusId, activeFocusId, onFocus)}
            className={focusClass(
              activeFocusId === fieldFocusId,
              'w-full rounded-md border px-2.5 py-1.5 font-mono text-xs',
            )}
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          >
            {(field.options ?? []).map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        ) : field.type === 'int' ? (
          <input
            id={fieldFocusId}
            type="number"
            value={Number.isNaN(Number(rawValue)) ? 0 : Number(rawValue)}
            onChange={(e) => {
              const parsed = Number.parseInt(e.target.value, 10);
              onUpdateField(field.key, Number.isNaN(parsed) ? 0 : parsed);
            }}
            data-focused={activeFocusId === fieldFocusId ? 'true' : undefined}
            {...focusableInputProps(fieldFocusId, activeFocusId, onFocus)}
            className={focusClass(
              activeFocusId === fieldFocusId,
              'w-full rounded-md border px-2.5 py-1.5 font-mono text-xs',
            )}
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          />
        ) : field.type === 'secret' ? (
          <div className="flex items-center gap-1.5">
            <input
              id={fieldFocusId}
              type={isRevealed ? 'text' : 'password'}
              value={String(rawValue)}
              onChange={(e) => onUpdateField(field.key, e.target.value)}
              placeholder="输入密钥（仅存入 0600 SecretVault）"
              data-focused={activeFocusId === fieldFocusId ? 'true' : undefined}
              {...focusableInputProps(fieldFocusId, activeFocusId, onFocus)}
              className={focusClass(
                activeFocusId === fieldFocusId,
                'min-w-0 flex-1 rounded-md border px-2.5 py-1.5 font-mono text-xs',
              )}
              style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
            />
            <FocusButton
              focusId={maskFocusId}
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={() => onToggleSecretReveal(secretKey)}
              surface="soft"
              className="shrink-0 px-2 py-1.5 font-mono text-[10px]"
            >
              {isRevealed ? '隐藏' : '显示'}
            </FocusButton>
          </div>
        ) : (
          <input
            id={fieldFocusId}
            type="text"
            value={String(rawValue)}
            onChange={(e) => onUpdateField(field.key, e.target.value)}
            data-focused={activeFocusId === fieldFocusId ? 'true' : undefined}
            {...focusableInputProps(fieldFocusId, activeFocusId, onFocus)}
            className={focusClass(
              activeFocusId === fieldFocusId,
              'w-full rounded-md border px-2.5 py-1.5 font-mono text-xs',
            )}
            style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
          />
        )}

        {field.description ? (
          <p className="text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
            {field.description}
          </p>
        ) : null}
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold">Schema 配置表单</div>
        <span className="font-mono text-[10px]" style={{ color: 'var(--text-muted, #66625D)' }}>
          {plugin.config_schema.length} fields
        </span>
      </div>

      <div className="flex flex-col gap-2.5">
        {plugin.config_schema.map(renderField)}
      </div>

      <div className="mt-1 flex items-center gap-2 border-t pt-3" style={{ borderColor: HAIRLINE_BORDER }}>
        <FocusButton
          focusId="store:form:save"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onSave}
          selected
          tone="core"
          className="flex-1 py-2 text-xs font-semibold"
        >
          保存配置 (0600)
        </FocusButton>
        <FocusButton
          focusId="store:form:reset"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onReset}
          surface="soft"
          className="px-3 py-2 text-xs font-medium"
        >
          重置默认
        </FocusButton>
      </div>
    </div>
  );
};

export default PluginConfigPanel;
