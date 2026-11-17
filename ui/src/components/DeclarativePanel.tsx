import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, ChevronRight, LoaderCircle, Play } from 'lucide-react';
import type { FocusZone } from '../types';
import { FocusItem } from './FocusItem';
import { MarkdownRenderer } from './MarkdownRenderer';

type Dict = Record<string, unknown>;

export interface DeclarativePanelProps {
  schema: unknown;
  focusPrefix: string;
  zone?: FocusZone;
  compact?: boolean;
  busy?: boolean;
  onAction: (actionId: string, state: Dict) => void;
}

const isRecord = (value: unknown): value is Dict =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const asNodes = (schema: unknown): Dict[] => {
  if (Array.isArray(schema)) return schema.filter(isRecord);
  return isRecord(schema) ? [schema] : [];
};

const textValue = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

interface SelectOption {
  value: string;
  label: string;
}

function normalizeOptions(raw: unknown): SelectOption[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry) => {
      if (isRecord(entry)) {
        const value = textValue(entry.value);
        return value ? { value, label: textValue(entry.label) || value } : null;
      }
      const value = textValue(entry);
      return value ? { value, label: value } : null;
    })
    .filter((entry): entry is SelectOption => entry !== null);
}

function safeImageSrc(value: unknown): string {
  const src = textValue(value).trim();
  if (src.startsWith('data:image/') || src.startsWith('https://')) return src;
  return '';
}

function safeImageWidth(value: unknown): number {
  const width = Number(value);
  return Number.isFinite(width) ? Math.max(80, Math.min(640, Math.round(width))) : 220;
}

function collectFieldDefaults(schema: unknown): Dict {
  const defaults: Dict = {};
  const visit = (node: unknown) => {
    if (Array.isArray(node)) {
      node.forEach(visit);
      return;
    }
    if (!isRecord(node)) return;
    if (String(node.type ?? '').toLowerCase() === 'form' && Array.isArray(node.fields)) {
      for (const rawField of node.fields) {
        if (!isRecord(rawField)) continue;
        const name = textValue(rawField.name);
        if (!name) continue;
        const type = textValue(rawField.type || 'string').toLowerCase();
        const rawDefault = rawField.default;
        if (type === 'bool' || type === 'boolean') {
          defaults[name] = Boolean(rawDefault);
        } else if (type === 'int' || type === 'number') {
          const number = Number(rawDefault);
          defaults[name] = Number.isFinite(number) ? number : 0;
        } else {
          defaults[name] = textValue(rawDefault);
        }
      }
    }
    visit(node.children);
  };
  visit(schema);
  return defaults;
}

function SurfaceCard({
  children,
  compact,
}: {
  children: ReactNode;
  compact: boolean;
}) {
  return (
    <section
      className={`rounded-[13px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] ${
        compact ? 'p-3' : 'p-4.5'
      }`}
      style={{ boxShadow: 'var(--shadow-page)' }}
    >
      {children}
    </section>
  );
}

function NodeHeading({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <header className="mb-3">
      <h3 className="text-[15.5px] font-semibold tracking-tight text-[var(--text-primary)]">
        {title}
      </h3>
      {description ? (
        <p className="mt-1 text-[13.5px] leading-[1.55] text-[var(--text-muted)]">
          {description}
        </p>
      ) : null}
    </header>
  );
}

function FieldControl({
  field,
  value,
  focusId,
  zone,
  compact,
  onChange,
}: {
  field: Dict;
  value: unknown;
  focusId: string;
  zone: FocusZone;
  compact: boolean;
  onChange: (value: unknown) => void;
}) {
  const inputRef = useRef<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null>(null);
  const name = textValue(field.name);
  const label = textValue(field.label || field.name);
  const type = textValue(field.type || 'string').toLowerCase();
  const description = textValue(field.description);
  const options = normalizeOptions(field.options);
  const controlClass =
    'w-full rounded-[9px] border border-[var(--border-line)] bg-[var(--bg-canvas)] px-3 py-2 text-[14.5px] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-subtle)] focus:border-[var(--focus-ring)]';

  return (
    <FocusItem
      as="div"
      id={focusId}
      zone={zone}
      label={`${label} 输入控件`}
      onActivate={() => inputRef.current?.focus()}
      className="rounded-[10px] p-1 -m-1"
    >
      <label className="block">
        <span className="mb-1.5 flex items-center justify-between gap-2 text-[13.5px] font-medium text-[var(--text-secondary)]">
          <span>{label}</span>
          {field.required ? (
            <span className="font-mono text-[12.5px] text-[var(--status-warn-fg)]">required</span>
          ) : null}
        </span>
        {type === 'select' ? (
          <select
            ref={(node) => {
              inputRef.current = node;
            }}
            value={textValue(value)}
            onChange={(event) => onChange(event.target.value)}
            aria-label={label}
            tabIndex={-1}
            className={controlClass}
          >
            {options.length === 0 ? <option value={textValue(value)}>{textValue(value) || '—'}</option> : null}
            {options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        ) : type === 'bool' || type === 'boolean' ? (
          <span className="flex h-9 items-center gap-2 text-[14px] text-[var(--text-muted)]">
            <input
              ref={(node) => {
                inputRef.current = node;
              }}
              type="checkbox"
              checked={Boolean(value)}
              onChange={(event) => onChange(event.target.checked)}
              aria-label={label}
              tabIndex={-1}
              className="h-4 w-4 accent-[var(--focus-ring)]"
            />
            <span>{Boolean(value) ? '已启用' : '未启用'}</span>
          </span>
        ) : type === 'textarea' ? (
          <textarea
            ref={(node) => {
              inputRef.current = node;
            }}
            rows={compact ? 2 : 3}
            value={textValue(value)}
            onChange={(event) => onChange(event.target.value)}
            aria-label={label}
            tabIndex={-1}
            className={`${controlClass} resize-y`}
          />
        ) : (
          <input
            ref={(node) => {
              inputRef.current = node;
            }}
            type={type === 'secret' ? 'password' : type === 'int' || type === 'number' ? 'number' : 'text'}
            value={textValue(value)}
            onChange={(event) =>
              onChange(
                type === 'int' || type === 'number'
                  ? Number(event.target.value)
                  : event.target.value,
              )
            }
            autoComplete={type === 'secret' ? 'new-password' : 'off'}
            aria-label={label}
            placeholder={textValue(field.placeholder)}
            tabIndex={-1}
            className={controlClass}
          />
        )}
        {description ? (
          <span className="mt-1 block text-[12.75px] leading-[1.45] text-[var(--text-subtle)]">
            {description}
          </span>
        ) : null}
        <span className="sr-only">{name}</span>
      </label>
    </FocusItem>
  );
}

function SchemaNode({
  node,
  path,
  values,
  setValue,
  onAction,
  focusPrefix,
  zone,
  compact,
  busy,
}: {
  node: Dict;
  path: string;
  values: Dict;
  setValue: (name: string, value: unknown) => void;
  onAction: (actionId: string, state: Dict) => void;
  focusPrefix: string;
  zone: FocusZone;
  compact: boolean;
  busy: boolean;
}) {
  const type = textValue(node.type || node.kind || 'Unknown');
  const id = textValue(node.id) || path;
  const title = textValue(node.title);
  const description = textValue(node.description);
  const lower = type.toLowerCase();

  if (lower === 'section') {
    return (
      <SurfaceCard compact={compact}>
        {title || description ? <NodeHeading title={title} description={description} /> : null}
        <div className={compact ? 'space-y-2.5' : 'space-y-3.5'}>
          {asNodes(node.children).map((child, index) => (
            <SchemaNode
              key={`${textValue(child.id) || child.type || 'node'}-${index}`}
              node={child}
              path={`${id}:${index}`}
              values={values}
              setValue={setValue}
              onAction={onAction}
              focusPrefix={focusPrefix}
              zone={zone}
              compact={compact}
              busy={busy}
            />
          ))}
        </div>
      </SurfaceCard>
    );
  }

  if (lower === 'markdown') {
    return (
      <SurfaceCard compact={compact}>
        {title ? <NodeHeading title={title} description={description} /> : null}
        <MarkdownRenderer
          content={textValue(node.content || node.text)}
          compact={compact}
          className="text-[var(--text-primary)]"
        />
      </SurfaceCard>
    );
  }

  if (lower === 'image') {
    const src = safeImageSrc(node.src ?? node.url);
    return (
      <SurfaceCard compact={compact}>
        {title || description ? <NodeHeading title={title} description={description} /> : null}
        {src ? (
          <img
            src={src}
            alt={textValue(node.alt) || title || '插件图片'}
            width={safeImageWidth(node.width)}
            className="mx-auto block h-auto max-w-full rounded-[10px] border border-[var(--border-subtle)] bg-white"
          />
        ) : (
          <p className="rounded-[9px] bg-[var(--bg-canvas)] px-3 py-4 text-center text-[13.5px] text-[var(--text-muted)]">
            插件图片地址不受支持
          </p>
        )}
      </SurfaceCard>
    );
  }

  if (lower === 'keyvalue') {
    const items = Array.isArray(node.items) ? node.items.filter(isRecord) : [];
    return (
      <SurfaceCard compact={compact}>
        {title || description ? <NodeHeading title={title} description={description} /> : null}
        <dl className="divide-y divide-[var(--border-subtle)]">
          {items.map((item, index) => (
            <div key={`${textValue(item.key)}-${index}`} className="flex items-start justify-between gap-4 py-2">
              <dt className="text-[13.5px] text-[var(--text-muted)]">
                {textValue(item.label || item.key)}
              </dt>
              <dd className="min-w-0 break-words text-right text-[13.5px] font-medium text-[var(--text-primary)]">
                {textValue(item.value)}
              </dd>
            </div>
          ))}
        </dl>
      </SurfaceCard>
    );
  }

  if (lower === 'form') {
    const fields = Array.isArray(node.fields) ? node.fields.filter(isRecord) : [];
    return (
      <SurfaceCard compact={compact}>
        {title || description ? <NodeHeading title={title} description={description} /> : null}
        <div className={`grid gap-2.5 ${compact ? 'grid-cols-1' : 'grid-cols-1 md:grid-cols-2'}`}>
          {fields.map((field, index) => {
            const name = textValue(field.name);
            return (
              <FieldControl
                key={`${name}-${index}`}
                field={field}
                value={values[name]}
                focusId={`${focusPrefix}:${id}:field:${name || index}`}
                zone={zone}
                compact={compact}
                onChange={(value) => setValue(name, value)}
              />
            );
          })}
        </div>
      </SurfaceCard>
    );
  }

  if (lower === 'actionrow') {
    const actions = Array.isArray(node.actions) ? node.actions.filter(isRecord) : [];
    return (
      <div className="flex flex-wrap items-center gap-2">
        {actions.map((action, index) => {
          const actionId = textValue(action.id);
          const variant = textValue(action.variant || 'secondary').toLowerCase();
          const primary = variant === 'primary';
          return (
            <FocusItem
              key={`${actionId}-${index}`}
              as="button"
              id={`${focusPrefix}:${id}:action:${actionId || index}`}
              zone={zone}
              label={textValue(action.label || actionId)}
              disabled={busy || !actionId}
              onActivate={() => onAction(actionId, values)}
              className={[
                'inline-flex cursor-pointer items-center gap-1.5 rounded-[9px] border px-3 py-2 text-[13.75px] font-medium transition-colors',
                primary
                  ? 'border-[var(--focus-ring)] bg-[var(--focus-fill)] text-[var(--text-primary)] hover:brightness-95'
                  : 'border-[var(--border-line)] bg-[var(--bg-soft)] text-[var(--text-primary)] hover:bg-[var(--hover-fill)]',
                busy ? 'cursor-wait opacity-55' : '',
              ].join(' ')}
            >
              {busy ? (
                <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
              ) : primary ? (
                <Play className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              <span>{textValue(action.label || actionId)}</span>
            </FocusItem>
          );
        })}
      </div>
    );
  }

  if (lower === 'list') {
    const items = Array.isArray(node.items) ? node.items.filter(isRecord) : [];
    return (
      <SurfaceCard compact={compact}>
        {title || description ? <NodeHeading title={title} description={description} /> : null}
        {items.length === 0 ? (
          <p className="rounded-[9px] bg-[var(--bg-canvas)] px-3 py-4 text-center text-[13.5px] text-[var(--text-muted)]">
            暂无条目
          </p>
        ) : (
          <ol className="space-y-1">
            {items.map((item, index) => (
              <li
                key={`${textValue(item.id)}-${index}`}
                className="rounded-[9px] px-2.5 py-2 hover:bg-[var(--hover-fill)]"
              >
                <div className="flex items-start gap-2.5">
                  <span className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[var(--bg-soft)] font-mono text-[12.5px] text-[var(--text-muted)]">
                    {index + 1}
                  </span>
                  <div className="min-w-0">
                    <div className="text-[14px] font-medium leading-[1.45] text-[var(--text-primary)]">
                      {textValue(item.title || item.label || item.id)}
                    </div>
                    {item.subtitle ? (
                      <div className="mt-0.5 line-clamp-2 text-[13px] leading-[1.45] text-[var(--text-muted)]">
                        {textValue(item.subtitle)}
                      </div>
                    ) : null}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        )}
      </SurfaceCard>
    );
  }

  return (
    <SurfaceCard compact={compact}>
      <NodeHeading
        title={title || `插件视图 · ${type}`}
        description={
          description ||
          '该插件声明了自定义视图类型；其动作仍可通过下方按钮调用。'
        }
      />
      <div className="flex flex-wrap gap-2">
        {['status', 'refresh'].map((actionId) => (
          <FocusItem
            key={actionId}
            as="button"
            id={`${focusPrefix}:${id}:action:${actionId}`}
            zone={zone}
            label={actionId}
            disabled={busy}
            onActivate={() => onAction(actionId, values)}
            className="cursor-pointer rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-soft)] px-2.5 py-1.5 text-[13px] text-[var(--text-primary)] hover:bg-[var(--hover-fill)]"
          >
            {actionId === 'status' ? '读取状态' : '刷新'}
          </FocusItem>
        ))}
      </div>
    </SurfaceCard>
  );
}

export function DeclarativePanel({
  schema,
  focusPrefix,
  zone = 'main',
  compact = false,
  busy = false,
  onAction,
}: DeclarativePanelProps) {
  const defaults = useMemo(() => collectFieldDefaults(schema), [schema]);
  const [values, setValues] = useState<Dict>(defaults);

  useEffect(() => {
    setValues((previous) => {
      const next: Dict = { ...defaults };
      for (const [key, value] of Object.entries(previous)) {
        if (key in defaults) next[key] = value;
      }
      return next;
    });
  }, [defaults]);

  const setValue = (name: string, value: unknown) => {
    if (!name) return;
    setValues((previous) => ({ ...previous, [name]: value }));
  };

  if (asNodes(schema).length === 0) {
    return (
      <div className="flex items-start gap-2 rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3.5 py-3 text-[13.5px] text-[var(--text-muted)]">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>插件尚未声明可渲染的 UI Schema。</span>
      </div>
    );
  }

  return (
    <div className={compact ? 'space-y-2.5' : 'space-y-3.5'}>
      {asNodes(schema).map((node, index) => (
        <SchemaNode
          key={`${textValue(node.id) || textValue(node.type) || 'root'}-${index}`}
          node={node}
          path={`root:${index}`}
          values={values}
          setValue={setValue}
          onAction={onAction}
          focusPrefix={focusPrefix}
          zone={zone}
          compact={compact}
          busy={busy}
        />
      ))}
    </div>
  );
}

export default DeclarativePanel;
