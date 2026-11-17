import React from 'react';
import { Lock } from 'lucide-react';
import StateView from '../../components/StateView';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableInputProps,
} from '../../components/WorkbenchLayout';
import { PanelSectionHeading, TagChip } from '../../components/WorkbenchPrimitives';
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
  const generalFields = plugin.config_schema.filter(
    (f) => f.type === 'string' || f.type === 'select' || f.type === 'int',
  );
  const secretFields = plugin.config_schema.filter((f) => f.type === 'secret');
  const toggleFields = plugin.config_schema.filter((f) => f.type === 'bool');

  const renderFieldHeader = (field: ConfigFieldSchema, fieldFocusId: string) => (
    <div className="flex items-center justify-between gap-2">
      <label htmlFor={fieldFocusId} className="text-[14.5px] font-semibold text-[var(--text-primary)]">
        {field.label}
        {field.required ? (
          <span style={{ color: 'var(--status-err-fg, #AA1D33)' }}> *</span>
        ) : null}
      </label>
      <span className="font-mono text-[13.5px] text-[var(--text-subtle)]">{field.key}</span>
    </div>
  );

  const renderStandardField = (field: ConfigFieldSchema) => {
    const fieldFocusId = `store:field:${plugin.id}:${field.key}`;
    const rawValue = configValues[field.key] ?? field.default;

    return (
      <div key={field.key} className="flex flex-col gap-1.5">
        {renderFieldHeader(field, fieldFocusId)}

        {field.type === 'select' ? (
          <select
            id={fieldFocusId}
            value={String(rawValue)}
            onChange={(e) => onUpdateField(field.key, e.target.value)}
            data-focused={activeFocusId === fieldFocusId ? 'true' : undefined}
            {...focusableInputProps(fieldFocusId, activeFocusId, onFocus)}
            className={focusClass(
              activeFocusId === fieldFocusId,
              'w-full rounded-[10px] border px-3 py-2 font-mono text-[14.5px]',
            )}
            style={{
              backgroundColor: 'var(--bg-canvas, #F6F5F3)',
              borderColor: 'var(--border-line)',
              color: 'var(--text-primary)',
            }}
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
              'w-full rounded-[10px] border px-3 py-2 font-mono text-[14.5px]',
            )}
            style={{
              backgroundColor: 'var(--bg-canvas, #F6F5F3)',
              borderColor: 'var(--border-line)',
              color: 'var(--text-primary)',
            }}
          />
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
              'w-full rounded-[10px] border px-3 py-2 font-mono text-[14.5px]',
            )}
            style={{
              backgroundColor: 'var(--bg-canvas, #F6F5F3)',
              borderColor: 'var(--border-line)',
              color: 'var(--text-primary)',
            }}
          />
        )}

        {field.description ? (
          <p className="text-[13.5px] leading-[1.6] text-[var(--text-muted)]">
            {field.description}
          </p>
        ) : null}
      </div>
    );
  };

  return (
    <div
      className="flex flex-col gap-6 rounded-[18px] p-6"
      style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
    >
      <PanelSectionHeading
        title="配置参数分组"
        meta={`${plugin.config_schema.length} 项 Schema`}
      />

      {plugin.config_schema.length === 0 ? (
        <StateView
          mode="empty"
          variant="inline"
          title="无配置项"
          description="该插件声明为空 Schema，开箱即用。"
        />
      ) : (
        <div className="flex flex-col gap-6">
          {/* Group 1: General & Connection Parameters */}
          {generalFields.length > 0 ? (
            <section aria-label="常规与连接参数" className="flex flex-col gap-3.5">
              <div className="text-[14px] font-semibold tracking-wide text-[var(--text-muted)]">
                常规与连接参数
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {generalFields.map(renderStandardField)}
              </div>
            </section>
          ) : null}

          {/* Group 2: Isolated 0600 SecretVault Credentials */}
          {secretFields.length > 0 ? (
            <section
              aria-label="安全凭据 (0600 SecretVault)"
              className="flex flex-col gap-3.5 rounded-[14px] p-4.5"
              style={{ backgroundColor: 'var(--bg-canvas)' }}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="inline-flex items-center gap-1.5 text-[14px] font-semibold text-[var(--text-primary)]">
                  <Lock className="h-3.5 w-3.5 text-[var(--domain-core)]" aria-hidden="true" />
                  <span>安全凭据与令牌</span>
                </span>
                <TagChip tone="warn" mono>
                  0600 SecretVault
                </TagChip>
              </div>

              <div className="flex flex-col gap-3.5">
                {secretFields.map((field) => {
                  const fieldFocusId = `store:field:${plugin.id}:${field.key}`;
                  const maskFocusId = `store:field-mask:${plugin.id}:${field.key}`;
                  const rawValue = configValues[field.key] ?? field.default;
                  const secretKey = `${plugin.id}:${field.key}`;
                  const isRevealed = Boolean(revealedSecrets[secretKey]);

                  return (
                    <div key={field.key} className="flex flex-col gap-1.5">
                      {renderFieldHeader(field, fieldFocusId)}
                      <div className="flex items-center gap-2">
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
                            'min-w-0 flex-1 rounded-[10px] border px-3 py-2 font-mono text-[14.5px]',
                          )}
                          style={{
                            backgroundColor: 'var(--bg-surface)',
                            borderColor: 'var(--border-line)',
                            color: 'var(--text-primary)',
                          }}
                        />
                        <FocusButton
                          focusId={maskFocusId}
                          activeFocusId={activeFocusId}
                          onFocus={onFocus}
                          onActivate={() => onToggleSecretReveal(secretKey)}
                          surface="soft"
                          className="shrink-0 px-3 py-2 text-[13.5px] font-medium"
                        >
                          {isRevealed ? '隐藏' : '显示'}
                        </FocusButton>
                      </div>
                      {field.description ? (
                        <p className="text-[13.5px] leading-[1.6] text-[var(--text-muted)]">
                          {field.description}
                        </p>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </section>
          ) : null}

          {/* Group 3: Feature Toggles & Policies */}
          {toggleFields.length > 0 ? (
            <section aria-label="行为开关与策略" className="flex flex-col gap-2.5">
              <div className="text-[14px] font-semibold tracking-wide text-[var(--text-muted)]">
                行为开关与策略
              </div>
              <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
                {toggleFields.map((field) => {
                  const fieldFocusId = `store:field:${plugin.id}:${field.key}`;
                  const checked = Boolean(configValues[field.key] ?? field.default);
                  return (
                    <FocusButton
                      key={field.key}
                      id={fieldFocusId}
                      focusId={fieldFocusId}
                      activeFocusId={activeFocusId}
                      onFocus={onFocus}
                      onActivate={() => onUpdateField(field.key, !checked)}
                      selected={checked}
                      tone="ok"
                      surface="canvas"
                      role="switch"
                      ariaChecked={checked}
                      className="flex items-center justify-between gap-3 px-3.5 py-2.5 text-left"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-[14.5px] font-semibold">
                          {field.label}
                        </span>
                        <span className="block font-mono text-[13.5px] opacity-75">
                          {field.key}
                        </span>
                      </span>
                      <span className="shrink-0 font-mono text-[13.5px] font-semibold">
                        {checked ? '已开启' : '已关闭'}
                      </span>
                    </FocusButton>
                  );
                })}
              </div>
            </section>
          ) : null}
        </div>
      )}

      <div
        className="flex flex-wrap items-center justify-end gap-2.5 border-t pt-4"
        style={{ borderColor: HAIRLINE_BORDER }}
      >
        <FocusButton
          focusId="store:form:reset"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onReset}
          surface="soft"
          className="px-4 py-2 text-[14px] font-medium"
        >
          重置默认
        </FocusButton>
        <FocusButton
          focusId="store:form:save"
          activeFocusId={activeFocusId}
          onFocus={onFocus}
          onActivate={onSave}
          selected
          tone="core"
          className="px-5 py-2 text-[14.5px] font-semibold"
        >
          保存配置 (0600)
        </FocusButton>
      </div>
    </div>
  );
};

export default PluginConfigPanel;
