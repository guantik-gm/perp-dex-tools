#!/usr/bin/env python3
"""
Hedged Position Analysis Script (Updated Version)
Analyzes trading records from Lighter and Edgex DEXs to calculate hedging friction
Key Updates:
- Timezone handling: Lighter=UTC, Edgex=UTC+8 (converted to UTC)
- Position aggregation: Orders within 2 minutes on same DEX = 1 position
- Hedge matching: Positions within 5 minutes across DEXs = hedge pair
"""

import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional
import sys

# Configuration
POSITION_AGGREGATION_MINUTES = 2  # Orders within 2 min = same position (both DEXs)
HEDGE_MATCHING_MINUTES = 30       # Positions within 30 min = hedge pair (手动操作)
LIGHTER_DATE_FILTER = datetime(2025, 10, 18)  # Filter Lighter records from this date


@dataclass
class Position:
    """Normalized position data structure"""
    dex: str  # 'Lighter' or 'Edgex'
    asset: str  # 'ETH' or 'BTC'
    direction: str  # 'Long' or 'Short'
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    total_fees: float
    open_time: datetime  # UTC
    close_time: Optional[datetime] = None  # UTC

    @property
    def net_pnl(self) -> float:
        """Net PnL after all fees"""
        return self.pnl - self.total_fees


@dataclass
class MatchedHedge:
    """Matched hedged position pair"""
    lighter_pos: Position
    edgex_pos: Position
    time_diff_minutes: float

    @property
    def total_friction(self) -> float:
        """Total hedging friction (combined net PnL)"""
        return self.lighter_pos.net_pnl + self.edgex_pos.net_pnl

    @property
    def friction_pct(self) -> float:
        """Friction as percentage of average position notional value"""
        lighter_notional = self.lighter_pos.size * self.lighter_pos.entry_price
        edgex_notional = self.edgex_pos.size * self.edgex_pos.entry_price
        avg_notional = (lighter_notional + edgex_notional) / 2
        return (self.total_friction / avg_notional) * 100 if avg_notional > 0 else 0


def parse_lighter_csv(filepath: str) -> pd.DataFrame:
    """
    Parse Lighter DEX CSV file (UTC timezone)
    Filters records from Oct 18, 2025 onwards
    """
    print(f"📖 Reading Lighter CSV: {filepath}")
    df = pd.read_csv(filepath)

    # Convert date column to datetime (already UTC)
    df['Date'] = pd.to_datetime(df['Date'])

    # Filter from Oct 18 onwards
    df = df[df['Date'] >= LIGHTER_DATE_FILTER].copy()

    # Convert numeric columns to float
    numeric_cols = ['Trade Value', 'Size', 'Price', 'Closed PnL', 'Fee']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    print(f"   ✓ Loaded {len(df)} records (UTC timezone)")
    return df


def parse_edgex_csv(filepath: str) -> pd.DataFrame:
    """
    Parse Edgex DEX CSV file with Chinese headers
    IMPORTANT: Converts UTC+8 to UTC timezone
    """
    print(f"📖 Reading Edgex CSV: {filepath}")
    df = pd.read_csv(filepath)

    # Map Chinese headers to English
    column_mapping = {
        '合约': 'Contract',
        '委托数量': 'Quantity',
        '入场价格': 'EntryPrice',
        '出场价格': 'ExitPrice',
        '交易类型': 'Type',
        '平仓盈亏': 'ClosedPnL',
        '开仓手续费': 'OpenFee',
        '平仓手续费': 'CloseFee',
        '资金费用': 'FundingFee',
        '出场类型': 'ExitType',
        '订单时间': 'OrderTime'
    }

    df = df.rename(columns=column_mapping)

    # Clean Quantity column (remove any crypto symbol suffix)
    df['Quantity'] = df['Quantity'].astype(str).str.replace(r'\s+[A-Z]+$', '', regex=True).str.replace(',', '').astype(float)

    # Clean numeric columns (remove commas and convert)
    numeric_cols = ['EntryPrice', 'ExitPrice', 'ClosedPnL', 'OpenFee', 'CloseFee', 'FundingFee']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(',', '').str.replace('+', '').astype(float)

    # Convert date from UTC+8 to UTC
    df['OrderTime'] = pd.to_datetime(df['OrderTime'])
    df['OrderTime_UTC'] = df['OrderTime'] - timedelta(hours=8)

    print(f"   ✓ Loaded {len(df)} records (converted UTC+8 → UTC)")
    return df


def aggregate_lighter_orders(df: pd.DataFrame) -> List[Position]:
    """
    Aggregate Lighter orders into complete positions
    Orders within 2 minutes on same asset/direction = 1 position
    """
    print("\n🔧 Aggregating Lighter orders into positions (2-min window)...")
    positions = []

    for market in df['Market'].unique():
        market_df = df[df['Market'] == market].copy()
        market_df = market_df.sort_values('Date')

        # Process both Long and Short positions
        for direction in ['Long', 'Short']:
            # Get all open and close orders
            open_orders = market_df[market_df['Side'] == f'Open {direction}'].copy()
            close_orders = market_df[market_df['Side'] == f'Close {direction}'].copy()

            # Group open orders by 2-minute time windows
            processed_open_indices = set()
            processed_close_indices = set()

            for idx, anchor_open in open_orders.iterrows():
                if idx in processed_open_indices:
                    continue

                anchor_time = anchor_open['Date']

                # Find all opens within 2 minutes
                window_opens = open_orders[
                    (open_orders['Date'] >= anchor_time - timedelta(minutes=POSITION_AGGREGATION_MINUTES)) &
                    (open_orders['Date'] <= anchor_time + timedelta(minutes=POSITION_AGGREGATION_MINUTES)) &
                    (~open_orders.index.isin(processed_open_indices))
                ]

                # Find matching closes (look ahead up to 24 hours)
                window_closes = close_orders[
                    (close_orders['Date'] >= anchor_time) &
                    (close_orders['Date'] <= anchor_time + timedelta(hours=24)) &
                    (~close_orders.index.isin(processed_close_indices))
                ]

                if len(window_closes) == 0:
                    continue  # Skip if no closes found

                # Group closes within 2 minutes of first close
                first_close_time = window_closes['Date'].min()
                final_closes = window_closes[
                    (window_closes['Date'] >= first_close_time - timedelta(minutes=POSITION_AGGREGATION_MINUTES)) &
                    (window_closes['Date'] <= first_close_time + timedelta(minutes=POSITION_AGGREGATION_MINUTES))
                ]

                # Calculate aggregated position metrics
                total_open_size = window_opens['Size'].sum()
                total_close_size = final_closes['Size'].sum()

                # Weighted average prices
                avg_entry = (window_opens['Price'] * window_opens['Size']).sum() / total_open_size if total_open_size > 0 else 0
                avg_exit = (final_closes['Price'] * final_closes['Size']).sum() / total_close_size if total_close_size > 0 else 0

                # Total PnL and fees
                total_pnl = final_closes['Closed PnL'].sum()
                total_fees = window_opens['Fee'].sum() + final_closes['Fee'].sum()

                position = Position(
                    dex='Lighter',
                    asset=market,
                    direction=direction,
                    size=min(total_open_size, total_close_size),
                    entry_price=avg_entry,
                    exit_price=avg_exit,
                    pnl=total_pnl,
                    total_fees=total_fees,
                    open_time=window_opens['Date'].min(),
                    close_time=final_closes['Date'].max()
                )

                positions.append(position)

                # Mark as processed
                processed_open_indices.update(window_opens.index)
                processed_close_indices.update(final_closes.index)

    print(f"   ✓ Aggregated into {len(positions)} Lighter positions")
    return positions


def aggregate_by_entry_price(records: List[dict], time_limit: int = 60) -> List[dict]:
    """
    按入场价格聚合订单（识别分批平仓）

    关键逻辑：相同入场价格 + 同币种 + 同方向 = 同一仓位的分批平仓

    Args:
        records: 待聚合的记录列表
        time_limit: 最大时间跨度（分钟），默认60分钟

    Returns:
        聚合后的记录列表
    """
    # 按（币种、方向、入场价格）分组
    groups = {}

    for record in records:
        # 使用入场价格作为分组键（保留2位小数以处理浮点误差）
        entry_price_key = round(record['entry_price'], 2)
        key = (record['asset'], record['direction'], entry_price_key)

        if key not in groups:
            groups[key] = []
        groups[key].append(record)

    aggregated = []
    price_groups_found = 0

    for key, group in groups.items():
        if len(group) == 1:
            # 单个订单直接返回
            aggregated.append(group[0])
        else:
            # 检查时间跨度
            times = [r['close_time'] for r in group]
            time_span = (max(times) - min(times)).total_seconds() / 60

            if time_span <= time_limit:
                # 聚合分批订单
                price_groups_found += 1
                total_size = sum(r['size'] for r in group)
                total_pnl = sum(r['pnl'] for r in group)
                total_open_fee = sum(r['open_fee'] for r in group)
                total_close_fee = sum(r['close_fee'] for r in group)
                total_funding_fee = sum(r['funding_fee'] for r in group)

                # 加权平均出场价格
                weighted_exit = sum(r['exit_price'] * r['size'] for r in group) / total_size

                aggregated_record = {
                    'asset': key[0],
                    'direction': key[1],
                    'entry_price': key[2],  # 相同的入场价
                    'size': total_size,
                    'exit_price': weighted_exit,
                    'pnl': total_pnl,
                    'open_fee': total_open_fee,
                    'close_fee': total_close_fee,
                    'funding_fee': total_funding_fee,
                    'close_time': min(times)  # 使用最早时间
                }

                print(f"  🔗 价格聚合: {key[0]} {key[1]} @ ${key[2]:.2f}")
                print(f"     {len(group)}笔分批 跨度{time_span:.1f}分钟 总计{total_size:.4f}")

                aggregated.append(aggregated_record)
            else:
                # 时间跨度太大，不聚合
                aggregated.extend(group)

    if price_groups_found > 0:
        print(f"   ✓ 价格聚合: 发现 {price_groups_found} 组分批平仓")

    return aggregated


def convert_edgex_to_positions(df: pd.DataFrame) -> List[Position]:
    """
    Convert Edgex aggregated position data to Position objects
    IMPROVED: Pre-aggregate orders at the exact same second before time-window aggregation
    NOTE: Edgex '买入/卖出' is close action, not position direction!
    """
    print("\n🔧 Converting and aggregating Edgex positions...")

    # STEP 1: Pre-aggregate orders at the exact same second (fixes Match #14 issue)
    df['time_second'] = df['OrderTime_UTC'].dt.floor('S')  # Round to nearest second

    # Extract asset
    df['asset'] = df['Contract'].str.replace('USDT永续', '').str.replace('USDT', '').str.replace('USD', '')

    # Infer direction from close action
    df['direction'] = df['Type'].apply(lambda x: 'Short' if x == '买入' else 'Long')

    pre_aggregated = []
    same_second_groups = 0

    for (asset, direction, time_sec), group in df.groupby(['asset', 'direction', 'time_second']):
        if len(group) > 1:
            same_second_groups += 1
            print(f"  🔗 聚合同秒订单: {asset} {direction} @ {time_sec.strftime('%H:%M:%S')} ({len(group)}条)")

        # Aggregate all orders at this exact second
        total_qty = group['Quantity'].sum()

        record = {
            'asset': asset,
            'direction': direction,
            'size': total_qty,
            'entry_price': (group['EntryPrice'] * group['Quantity']).sum() / total_qty,
            'exit_price': (group['ExitPrice'] * group['Quantity']).sum() / total_qty,
            'pnl': group['ClosedPnL'].sum(),
            'open_fee': group['OpenFee'].sum(),
            'close_fee': group['CloseFee'].sum(),
            'funding_fee': group['FundingFee'].sum(),
            'close_time': time_sec
        }
        pre_aggregated.append(record)

    if same_second_groups > 0:
        print(f"   ✓ 预聚合: {len(df)} 条原始订单 → {len(pre_aggregated)} 个同秒聚合仓位")

    # STEP 2: Price-based aggregation (识别分批平仓)
    pre_aggregated = aggregate_by_entry_price(pre_aggregated, time_limit=60)

    # STEP 3: Time-window aggregation (2-minute window)
    positions = []
    processed_indices = set()

    for i, anchor in enumerate(pre_aggregated):
        if i in processed_indices:
            continue

        anchor_time = anchor['close_time']

        # Find all positions within 2 minutes with same asset and direction
        group_indices = []
        for j, pos in enumerate(pre_aggregated):
            if j in processed_indices:
                continue

            if (pos['asset'] == anchor['asset'] and
                pos['direction'] == anchor['direction'] and
                abs((pos['close_time'] - anchor_time).total_seconds()) <= POSITION_AGGREGATION_MINUTES * 60):
                group_indices.append(j)

        # Aggregate the group
        if group_indices:
            total_size = sum(pre_aggregated[idx]['size'] for idx in group_indices)
            total_pnl = sum(pre_aggregated[idx]['pnl'] for idx in group_indices)
            total_open_fee = sum(pre_aggregated[idx]['open_fee'] for idx in group_indices)
            total_close_fee = sum(pre_aggregated[idx]['close_fee'] for idx in group_indices)
            total_funding_fee = sum(pre_aggregated[idx]['funding_fee'] for idx in group_indices)

            # Weighted average prices
            weighted_entry = sum(pre_aggregated[idx]['entry_price'] * pre_aggregated[idx]['size']
                                for idx in group_indices) / total_size
            weighted_exit = sum(pre_aggregated[idx]['exit_price'] * pre_aggregated[idx]['size']
                               for idx in group_indices) / total_size

            # Use earliest close time
            earliest_close = min(pre_aggregated[idx]['close_time'] for idx in group_indices)

            position = Position(
                dex='Edgex',
                asset=anchor['asset'],
                direction=anchor['direction'],
                size=total_size,
                entry_price=weighted_entry,
                exit_price=weighted_exit,
                pnl=total_pnl,
                total_fees=total_open_fee + total_close_fee + total_funding_fee,
                open_time=earliest_close,  # This is actually close time
                close_time=earliest_close
            )

            positions.append(position)
            processed_indices.update(group_indices)

    print(f"   ✓ 最终聚合: {len(pre_aggregated)} 个同秒仓位 → {len(positions)} 个Edgex仓位")
    return positions


def match_positions(lighter_positions: List[Position],
                   edgex_positions: List[Position]) -> tuple[List[MatchedHedge], List[Position], List[Position]]:
    """
    Match hedged positions between Lighter and Edgex
    Criteria: same asset, opposite directions, close times within 30 minutes
    NOTE: Edgex CSV only has close time, not open time!
    Returns: (matches, unmatched_lighter, unmatched_edgex)
    """
    print(f"\n🔗 Matching hedge pairs (30-min close time window, opposite directions)...")
    matches = []
    matched_lighter_indices = set()
    matched_edgex_indices = set()

    for lighter_idx, lighter_pos in enumerate(lighter_positions):
        best_match = None
        best_score = float('inf')
        best_idx = None

        for idx, edgex_pos in enumerate(edgex_positions):
            if idx in matched_edgex_indices:
                continue

            # Must be same asset
            if lighter_pos.asset != edgex_pos.asset:
                continue

            # Must be opposite directions (hedge)
            if lighter_pos.direction == edgex_pos.direction:
                continue

            # Check close time proximity (5 minutes)
            # Edgex open_time is actually close time (CSV limitation)
            if lighter_pos.close_time is None:
                continue

            time_diff = abs((lighter_pos.close_time - edgex_pos.open_time).total_seconds() / 60)
            if time_diff > HEDGE_MATCHING_MINUTES:
                continue

            # Calculate match score (prefer closer time match and size)
            size_diff_pct = abs(lighter_pos.size - edgex_pos.size) / max(lighter_pos.size, edgex_pos.size) * 100
            score = time_diff + (size_diff_pct * 0.01)  # Heavily prioritize time match

            if score < best_score:
                best_score = score
                best_match = edgex_pos
                best_idx = idx
                best_time_diff = time_diff

        if best_match:
            matches.append(MatchedHedge(
                lighter_pos=lighter_pos,
                edgex_pos=best_match,
                time_diff_minutes=best_time_diff
            ))
            matched_lighter_indices.add(lighter_idx)
            matched_edgex_indices.add(best_idx)

    # Collect unmatched positions
    unmatched_lighter = [pos for idx, pos in enumerate(lighter_positions) if idx not in matched_lighter_indices]
    unmatched_edgex = [pos for idx, pos in enumerate(edgex_positions) if idx not in matched_edgex_indices]

    print(f"   ✓ Matched {len(matches)} hedge pairs")
    print(f"   ⚠ Unmatched: {len(unmatched_lighter)} Lighter, {len(unmatched_edgex)} Edgex")

    return matches, unmatched_lighter, unmatched_edgex


def generate_report(matches: List[MatchedHedge], output_path: str):
    """
    Generate detailed friction analysis report
    """
    print(f"\n📊 Generating report...")

    report_data = []
    for i, match in enumerate(matches, 1):
        lighter_notional = match.lighter_pos.size * match.lighter_pos.entry_price
        edgex_notional = match.edgex_pos.size * match.edgex_pos.entry_price

        report_data.append({
            'Match_ID': i,
            'Asset': match.lighter_pos.asset,
            'Position_Size': f"{match.lighter_pos.size:.4f}",
            'Lighter_Notional_USDT': f"{lighter_notional:.2f}",
            'Edgex_Notional_USDT': f"{edgex_notional:.2f}",

            'Lighter_Direction': match.lighter_pos.direction,
            'Lighter_Entry': f"{match.lighter_pos.entry_price:.2f}",
            'Lighter_Exit': f"{match.lighter_pos.exit_price:.2f}",
            'Lighter_PnL': f"{match.lighter_pos.pnl:.2f}",
            'Lighter_Fees': f"{match.lighter_pos.total_fees:.2f}",
            'Lighter_Net': f"{match.lighter_pos.net_pnl:.2f}",

            'Edgex_Direction': match.edgex_pos.direction,
            'Edgex_Entry': f"{match.edgex_pos.entry_price:.2f}",
            'Edgex_Exit': f"{match.edgex_pos.exit_price:.2f}",
            'Edgex_PnL': f"{match.edgex_pos.pnl:.2f}",
            'Edgex_Fees': f"{match.edgex_pos.total_fees:.2f}",
            'Edgex_Net': f"{match.edgex_pos.net_pnl:.2f}",

            'Time_Diff_Minutes': f"{match.time_diff_minutes:.2f}",
            'Total_Friction_USDT': f"{match.total_friction:.2f}",
            'Friction_Pct': f"{match.friction_pct:.3f}%",

            'Lighter_Open_Time_UTC': match.lighter_pos.open_time.strftime('%Y-%m-%d %H:%M:%S'),
            'Edgex_Open_Time_UTC': match.edgex_pos.open_time.strftime('%Y-%m-%d %H:%M:%S'),
        })

    df_report = pd.DataFrame(report_data)
    df_report.to_csv(output_path, index=False)

    print(f"   ✓ Report saved to: {output_path}")

    # Warning about Lighter fees
    total_lighter_fees = sum(m.lighter_pos.total_fees for m in matches)
    if total_lighter_fees == 0:
        print(f"\n   ⚠️  WARNING: Lighter fees are all $0 in CSV data!")
        print(f"       Actual friction may be higher than reported.")

    # Print summary statistics
    print(f"\n📈 Summary Statistics:")
    print(f"   Total Matched Hedge Pairs: {len(matches)}")

    total_friction = sum(m.total_friction for m in matches)
    avg_friction = total_friction / len(matches) if matches else 0
    avg_friction_pct = sum(m.friction_pct for m in matches) / len(matches) if matches else 0

    print(f"   Total Hedging Friction: {total_friction:.2f} USDT")
    print(f"   Average Friction per Hedge: {avg_friction:.2f} USDT")
    print(f"   Average Friction %: {avg_friction_pct:.3f}%")

    # Break down by asset
    for asset in sorted(set(m.lighter_pos.asset for m in matches)):
        asset_matches = [m for m in matches if m.lighter_pos.asset == asset]
        asset_friction = sum(m.total_friction for m in asset_matches)
        asset_notional = sum(m.lighter_pos.size * m.lighter_pos.entry_price for m in asset_matches)
        asset_friction_pct = (asset_friction / asset_notional * 100) if asset_notional > 0 else 0
        print(f"   {asset}: {asset_friction:.2f} USDT ({len(asset_matches)} pairs, {asset_friction_pct:.3f}%)")


def main():
    """Main execution function"""
    print("=" * 70)
    print("Hedged Position Analysis - Lighter & Edgex DEX (v2)")
    print("Timezone: Lighter=UTC, Edgex=UTC+8→UTC")
    print("Position Aggregation: 2-min window | Hedge Matching: 30-min window")
    print("=" * 70)

    # File paths
    script_dir = "/Volumes/SSD1T/code/bc/perp-dex-tools/hedge_analysis"
    lighter_csv = f"{script_dir}/lighter-trade-export-2025-10-23T02_55_04.152Z-UTC.csv"
    edgex_csv = f"{script_dir}/Edgex-Derivatives-USDTPerp-ClosePnL-20251023111118.csv"
    output_csv = f"{script_dir}/hedge_friction_analysis.csv"

    try:
        # Step 1: Parse CSV files
        lighter_df = parse_lighter_csv(lighter_csv)
        edgex_df = parse_edgex_csv(edgex_csv)

        # Step 2: Aggregate/convert positions
        lighter_positions = aggregate_lighter_orders(lighter_df)
        edgex_positions = convert_edgex_to_positions(edgex_df)

        # Step 3: Match positions
        matches, unmatched_lighter, unmatched_edgex = match_positions(lighter_positions, edgex_positions)

        # Step 4: Generate report
        if matches:
            generate_report(matches, output_csv)
        else:
            print("\n⚠ No matched hedge pairs found!")

        # Step 5: Print unmatched positions
        if unmatched_lighter or unmatched_edgex:
            print("\n" + "=" * 70)
            print("🔍 UNMATCHED POSITIONS DETAILS")
            print("=" * 70)

            if unmatched_lighter:
                print(f"\n📍 Unmatched Lighter Positions ({len(unmatched_lighter)}):")
                print("-" * 70)
                for i, pos in enumerate(unmatched_lighter, 1):
                    print(f"{i}. {pos.asset} {pos.direction}")
                    print(f"   Size: {pos.size:.4f}")
                    print(f"   Open: {pos.open_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    print(f"   Close: {pos.close_time.strftime('%Y-%m-%d %H:%M:%S') if pos.close_time else 'N/A'} UTC")
                    print(f"   Entry: ${pos.entry_price:.2f} → Exit: ${pos.exit_price:.2f}")
                    print(f"   PnL: ${pos.pnl:.2f}, Fees: ${pos.total_fees:.2f}")
                    print()

            if unmatched_edgex:
                print(f"\n📍 Unmatched Edgex Positions ({len(unmatched_edgex)}):")
                print("-" * 70)
                for i, pos in enumerate(unmatched_edgex, 1):
                    print(f"{i}. {pos.asset} {pos.direction}")
                    print(f"   Size: {pos.size:.4f}")
                    print(f"   Close Time: {pos.open_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                    print(f"   Entry: ${pos.entry_price:.2f} → Exit: ${pos.exit_price:.2f}")
                    print(f"   PnL: ${pos.pnl:.2f}, Fees: ${pos.total_fees:.2f}")
                    print()

                # Analyze potential matching issues
                print("🔎 Unmatched Edgex Analysis:")
                print("-" * 70)

                for edgex_pos in unmatched_edgex:
                    # Find Lighter positions with same asset but different criteria
                    same_asset_lighter = [p for p in lighter_positions if p.asset == edgex_pos.asset]
                    same_direction_lighter = [p for p in same_asset_lighter if p.direction == edgex_pos.direction]
                    opposite_direction_lighter = [p for p in same_asset_lighter if p.direction != edgex_pos.direction]

                    print(f"\n{edgex_pos.asset} {edgex_pos.direction} @ {edgex_pos.open_time.strftime('%H:%M:%S')}")
                    print(f"  Same asset in Lighter: {len(same_asset_lighter)}")
                    print(f"  Same direction (not hedge): {len(same_direction_lighter)}")
                    print(f"  Opposite direction (potential hedge): {len(opposite_direction_lighter)}")

                    if opposite_direction_lighter:
                        print(f"  Closest Lighter {edgex_pos.asset} {opposite_direction_lighter[0].direction}:")
                        for lpos in opposite_direction_lighter:
                            if lpos.close_time:
                                time_diff = abs((lpos.close_time - edgex_pos.open_time).total_seconds() / 60)
                                size_diff = abs(lpos.size - edgex_pos.size)
                                print(f"    Close: {lpos.close_time.strftime('%H:%M:%S')} | "
                                      f"Time diff: {time_diff:.2f}min | Size diff: {size_diff:.4f}")

        print("\n" + "=" * 70)
        print("✅ Analysis Complete!")
        print("=" * 70)

    except FileNotFoundError as e:
        print(f"\n❌ Error: File not found - {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
