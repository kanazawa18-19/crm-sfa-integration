"use client";

import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

interface StatusCategoryChartProps {
  data: { name: string; value: number }[];
  colors: Record<string, string>;
}

export default function StatusCategoryChart({ data, colors }: StatusCategoryChartProps) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={90} label>
          {data.map((entry) => (
            <Cell key={entry.name} fill={colors[entry.name] ?? "#9ca3af"} />
          ))}
        </Pie>
        <Tooltip formatter={(value) => `${value}件`} />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
}
