export default function ErrorBanner({ message }: { message: string }) {
  return <div className="alert alert-danger">⚠ {message}</div>
}
