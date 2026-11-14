import React, { useEffect, useState } from 'react'
import { X, Settings, Volume2, Download, Sparkles, CheckCircle2 } from 'lucide-react'
import type { AppSettings } from '../types/ebook'
import { dbService } from '../services/db'
import {
  exportAllAiCache,
  getConnectionConfig,
  saveConnectionConfig,
  testFeiniuConnection,
} from '../services/ai'

interface SettingsModalProps {
  isOpen: boolean
  onClose: () => void
  onSettingsChanged?: () => void
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  onSettingsChanged,
}) => {
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [baseUrl, setBaseUrl] = useState('http://100.90.158.48:8766/v1')
  const [apiKey, setApiKey] = useState('')
  const [configured, setConfigured] = useState(false)
  const [statusMsg, setStatusMsg] = useState('')
  const [statusOk, setStatusOk] = useState<boolean | null>(null)
  const [testing, setTesting] = useState(false)

  useEffect(() => {
    if (isOpen) {
      dbService.getSettings().then(setSettings)
      const cfg = getConnectionConfig()
      setBaseUrl(cfg.baseUrl || 'http://100.90.158.48:8766/v1')
      setConfigured(cfg.configured)
      setApiKey('')
      setStatusMsg(
        cfg.configured
          ? '已保存飞牛 8766 令牌（与手机输入法模块「措辞」共用同一端口与令牌）'
          : '尚未配置飞牛访问令牌'
      )
      setStatusOk(cfg.configured ? true : null)
    }
  }, [isOpen])

  if (!isOpen || !settings) return null

  const handleUpdate = async <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => {
    const updated = { ...settings, [key]: value }
    setSettings(updated)
    await dbService.updateSetting(key, value)
    onSettingsChanged?.()
  }

  const handleSaveConnection = () => {
    const res = saveConnectionConfig(baseUrl, apiKey)
    if (!res.ok) {
      setStatusOk(false)
      setStatusMsg(res.error || '保存失败')
      return
    }
    setConfigured(res.config?.configured ?? false)
    setApiKey('')
    setStatusOk(true)
    setStatusMsg('飞牛连接设置已保存')
  }

  const handleTestConnection = async () => {
    if (apiKey.trim()) {
      const saved = saveConnectionConfig(baseUrl, apiKey)
      if (!saved.ok) {
        setStatusOk(false)
        setStatusMsg(saved.error || '保存失败')
        return
      }
      setConfigured(saved.config?.configured ?? false)
      setApiKey('')
    }
    setTesting(true)
    setStatusMsg('正在连接飞牛 8766 服务...')
    try {
      const res = await testFeiniuConnection()
      setStatusOk(res.ok)
      setStatusMsg(res.message)
    } finally {
      setTesting(false)
    }
  }

  const handleExportData = async () => {
    const allWords = await dbService.getAllWordRecords()
    const allNotes = await dbService.getAllSentenceNotes()
    const settingsData = await dbService.getSettings()
    const aiCache = await exportAllAiCache()
    const data = {
      version: 2,
      exportTime: new Date().toISOString(),
      sentenceNotes: allNotes,
      userWords: allWords,
      settings: settingsData,
      aiCache,
    }
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ebook-companion-backup-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4 bg-[#2e2a24]/45 backdrop-blur-xs animate-in fade-in"
    >
      <div
        className="w-full max-w-lg bg-[#fdfbf7] border border-[#ded6c6] rounded-3xl shadow-xl overflow-hidden flex flex-col text-[#2e2a24]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 sm:p-5 border-b border-[#e5decfe6] bg-[#f8f5ef] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Settings className="text-[#996021]" size={20} />
            <h2 className="text-lg sm:text-xl font-semibold text-[#2e2a24]">连接与伴读设置</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-xl bg-[#f1ece1] text-[#72685b] hover:text-[#2e2a24]"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-4 sm:p-6 space-y-6 overflow-y-auto max-h-[75vh] text-sm">
          <div className="p-4 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-[#996021] flex items-center gap-1.5">
                <Sparkles size={15} />
                <span>飞牛 6-luna 伴读与美剧台词接口（端口 8766）</span>
              </h3>
              {configured && (
                <span className="inline-flex items-center gap-1 text-[11px] text-[#496247] font-medium">
                  <CheckCircle2 size={13} />
                  已配置密钥
                </span>
              )}
            </div>

            <p className="text-xs text-[#72685b] leading-relaxed">
              与输入法模块「措辞」共用同一个飞牛 <code className="px-1 py-0.5 rounded bg-[#efe8da]">8766</code> 端口和 Token，用于电子书句子上下文深读、段落伴读精译、词源拆解与美剧台词检索。
            </p>

            <div className="space-y-2.5">
              <label className="block text-xs text-[#5c5347]">
                飞牛服务地址
                <input
                  type="text"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="http://100.90.158.48:8766/v1"
                  className="mt-1 w-full px-3 py-2 rounded-xl bg-[#fdfbf7] border border-[#ded6c6] text-sm text-[#2e2a24] focus:outline-hidden focus:border-[#996021]"
                />
              </label>

              <label className="block text-xs text-[#5c5347]">
                访问令牌（Bearer Token，留空则保留现有令牌）
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={configured ? '已加密保存，如需更换请输入新令牌' : '粘贴飞牛 8766 服务的 token'}
                  className="mt-1 w-full px-3 py-2 rounded-xl bg-[#fdfbf7] border border-[#ded6c6] text-sm text-[#2e2a24] focus:outline-hidden focus:border-[#996021]"
                />
              </label>
            </div>

            {statusMsg && (
              <div
                className={`text-xs px-3 py-2 rounded-xl border ${
                  statusOk === true
                    ? 'bg-[#e7f0e8] border-[#c5dbc8] text-[#35593d]'
                    : statusOk === false
                    ? 'bg-[#f8e5e2] border-[#e4bfb8] text-[#94352b]'
                    : 'bg-[#f1ece1] border-[#ded6c6] text-[#72685b]'
                }`}
              >
                {statusMsg}
              </div>
            )}

            <div className="flex items-center gap-2 pt-1">
              <button
                type="button"
                onClick={handleSaveConnection}
                className="flex-1 py-2.5 px-3 rounded-xl bg-[#996021] text-[#fdfbf7] font-medium text-xs hover:brightness-95 transition-all cursor-pointer"
              >
                保存设置
              </button>
              <button
                type="button"
                disabled={testing}
                onClick={handleTestConnection}
                className="flex-1 py-2.5 px-3 rounded-xl bg-[#fdfbf7] border border-[#ded6c6] text-[#5c5347] font-medium text-xs hover:bg-[#f1ece1] disabled:opacity-50 transition-all cursor-pointer"
              >
                {testing ? '正在测试...' : '测试飞牛连接'}
              </button>
            </div>
          </div>

          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-[#72685b] flex items-center gap-1.5">
              <Volume2 size={15} />
              语音合成朗读口音
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => handleUpdate('voiceAccent', 'us')}
                className={`p-3 rounded-2xl border flex flex-col items-center gap-1 transition-all cursor-pointer ${
                  settings.voiceAccent === 'us'
                    ? 'bg-[#efe2cc] border-[#c89d66] text-[#7a4b19]'
                    : 'bg-[#f8f5ef] border-[#ded6c6] text-[#72685b]'
                }`}
              >
                <span className="font-semibold text-base text-[#2e2a24]">美音 (US)</span>
                <span className="text-xs opacity-80">北美原版书刊与美剧发音</span>
              </button>
              <button
                type="button"
                onClick={() => handleUpdate('voiceAccent', 'uk')}
                className={`p-3 rounded-2xl border flex flex-col items-center gap-1 transition-all cursor-pointer ${
                  settings.voiceAccent === 'uk'
                    ? 'bg-[#efe2cc] border-[#c89d66] text-[#7a4b19]'
                    : 'bg-[#f8f5ef] border-[#ded6c6] text-[#72685b]'
                }`}
              >
                <span className="font-semibold text-base text-[#2e2a24]">英音 (UK)</span>
                <span className="text-xs opacity-80">英式标准清晰发音</span>
              </button>
            </div>
          </div>

          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-[#72685b] flex items-center gap-1.5">
              <Download size={15} />
              读书笔记与生词备份
            </h3>
            <button
              type="button"
              onClick={handleExportData}
              className="w-full flex items-center justify-center gap-2 py-2.5 px-4 rounded-2xl bg-[#f8f5ef] hover:bg-[#f1ece1] text-[#5c5347] border border-[#ded6c6] font-medium transition-colors cursor-pointer"
            >
              <Download size={16} />
              <span>导出读书笔记与生词本备份 (JSON)</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
