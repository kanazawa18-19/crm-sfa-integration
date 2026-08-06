"use client";

import { useRouter } from "next/navigation";

interface DatePickerProps {
  basePath: string;
  date: string;
}

export default function DatePicker({ basePath, date }: DatePickerProps) {
  const router = useRouter();

  return (
    <input
      type="date"
      value={date}
      onChange={(event) => {
        if (event.target.value) {
          router.push(`${basePath}?date=${event.target.value}`);
        }
      }}
      className="rounded border border-gray-300 px-3 py-1.5 text-sm text-gray-900 focus:border-blue-500 focus:outline-none"
    />
  );
}
