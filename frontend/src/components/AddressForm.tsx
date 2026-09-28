import { useId, useState, type FormEvent } from "react";

import { checkAddress } from "../lib/address";
import { EXAMPLES } from "../lib/examples";

type Props = {
  initial: string;
  busy: boolean;
  onSubmit: (address: string) => void;
};

export function AddressForm({ initial, busy, onSubmit }: Props) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const errorId = useId();

  function submit(address: string) {
    const check = checkAddress(address);
    if (!check.ok) {
      setError(check.error);
      return;
    }
    setError(null);
    onSubmit(check.address);
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    submit(value);
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-3">
      <label htmlFor={inputId} className="block text-sm font-medium text-slate-700">
        Ethereum wallet address
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id={inputId}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="0x…"
          spellCheck={false}
          autoComplete="off"
          aria-invalid={error !== null}
          aria-describedby={error ? errorId : undefined}
          className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-sm shadow-sm focus-visible:border-blue-600 focus-visible:outline-2 focus-visible:outline-blue-600"
        />
        <button
          type="submit"
          disabled={busy}
          className="rounded-lg bg-blue-700 px-5 py-2 font-medium text-white shadow-sm hover:bg-blue-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:cursor-wait disabled:opacity-60"
        >
          {busy ? "Analyzing…" : "Analyze"}
        </button>
      </div>
      {error && (
        <p id={errorId} role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
        <span>Try:</span>
        {EXAMPLES.map((example) => (
          <button
            key={example.address}
            type="button"
            onClick={() => {
              setValue(example.address);
              submit(example.address);
            }}
            className="rounded-full border border-slate-300 bg-white px-3 py-1 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-blue-700"
          >
            {example.label}
          </button>
        ))}
      </div>
    </form>
  );
}
