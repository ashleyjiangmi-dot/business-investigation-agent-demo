import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: '经营 Agent（Demo）',
  description: '本地演示页：提交经营问题、看真实调查事件、拿最终结果（Demo 模拟数据口径）',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  )
}
