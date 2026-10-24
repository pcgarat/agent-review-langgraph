import { PendingPane } from '@/components/PendingPane'

export function ReviewPage({ reviewKey }: { reviewKey: string }) {
  return (
    <>
      <p className="text-body-secondary small">
        <code>{reviewKey}</code>
      </p>
      <PendingPane block="F" />
    </>
  )
}
