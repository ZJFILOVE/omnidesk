// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { App } from '../../App';
import { playerShortcut } from '../../utils/playerShortcuts';
afterEach(cleanup);
it('maps player transport keys without consuming workbench modifier bindings', () => {
  const key = (value: string, shiftKey = false, ctrlKey = false) => playerShortcut({ key: value, shiftKey, ctrlKey, altKey: false, metaKey: false });
  expect(key('F9')).toBe('skip');
  expect(key('ArrowLeft')).toBe('back');
  expect(key('N', true)).toBe('next');
  expect(key('.')).toBe('faster');
  expect(key('。')).toBe('faster');
  expect(key(',')).toBe('slower');
  expect(key('，')).toBe('slower');
  expect(key('m', false, true)).toBeUndefined();
});
it('seeks on the media workbench and isolates typing and overlays', () => {
  render(<App />);
  fireEvent.keyDown(window, { key: 'ArrowRight' });
  expect(screen.getByText('离线预览跳转至 74s')).not.toBeNull();
  const input = screen.getAllByRole('searchbox')[0];
  fireEvent.keyDown(input, { key: 'ArrowRight' });
  expect(screen.queryByText('离线预览跳转至 84s')).toBeNull();
  fireEvent.keyDown(window, { key: '?' });
  fireEvent.keyDown(window, { key: 'ArrowRight' });
  expect(screen.queryByText('离线预览跳转至 84s')).toBeNull();
});
