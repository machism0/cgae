import torch
import torch.nn
import torch.nn.functional
import torch.optim
import torch.utils.data

from time import perf_counter
from tqdm import tqdm
from argparse import ArgumentParser

# from cgae.utils import write_traj, save_traj
from cgae.cgae_dense import gumbel_softmax, Encoder, Decoder
from cgae.cgae import temp_scheduler

import otp

parser = ArgumentParser(parents=[otp.otp_parser()])
args = otp.parse_args(parser)


def execute(args):
    # Data
    geometries, forces, features = otp.data(args)

    encoder = Encoder(
        in_dim=geometries.size(1), out_dim=args.ncg, hard=False, device=args.device
    ).to(args.device)
    decoder = Decoder(in_dim=args.ncg, out_dim=geometries.size(1)).to(args.device)
    optimizer = torch.optim.Adam(
        list(encoder.parameters()) + list(decoder.parameters()), lr=args.lr
    )
    temp_sched = temp_scheduler(
        args.epochs,
        args.tdr,
        args.temp,
        args.tmin,
        dtype=args.precision,
        device=args.device,
    )
    n_batches, geometries, forces, features = otp.batch(
        geometries, forces, features, args.bs
    )

    dynamics = []
    summaries = []
    wall_start = perf_counter()
    torch.manual_seed(args.seed)
    for epoch in tqdm(range(args.epochs)):
        for step, batch in tqdm(
            enumerate(torch.randperm(n_batches, device=args.device))
        ):
            _, geo, force = features[batch], geometries[batch], forces[batch]
            cg_xyz = encoder(geo, temp_sched[epoch])
            decoded = decoder(cg_xyz)
            loss_ae = (decoded - geo).pow(2).sum(-1).mean()

            if args.fm and epoch >= args.fm_epoch:
                cg_force_assign = gumbel_softmax(
                    encoder.weight1.t(),
                    temp_sched[epoch] * args.force_temp_coeff,
                    device=args.device,
                ).t()
                f = torch.matmul(cg_force_assign, force)
                loss_fm = f.pow(2).sum(-1).mean()

                loss = loss_ae + args.fm_co * loss_fm
            else:
                loss_fm = torch.tensor(0)
                loss = loss_ae

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            dynamics.append(
                {
                    "loss_ae": loss_ae.item(),
                    "loss_fm": loss_fm.item(),
                    "loss": loss.item(),
                    "epoch": epoch,
                    "step": step,
                    "batch": batch.item(),
                }
            )

        wall = perf_counter() - wall_start
        if wall > args.wall:
            break

        summaries.append(
            {
                "loss_ae": loss_ae.item(),
                "loss_fm": loss_fm.item(),
                "loss": loss.item(),
                "epoch": epoch,
                "step": step,
                "batch": batch.item(),
                "cg_xyz": cg_xyz,
                "temp": temp_sched[epoch].item(),
                "gumble": gumbel_softmax(
                    encoder.weight1.t(), temp_sched[epoch], device=args.device
                ),
                "st_gumble": gumbel_softmax(
                    encoder.weight1.t(),
                    temp_sched[epoch],
                    device=args.device,
                    hard=True,
                ),
                "reconstructed": decoded,
            }
        )

    return {
        "args": args,
        "dynamics": dynamics,
        "summaries": summaries,
        # 'train': {
        #     'pred': evaluate(f, features, geometry, train[:len(test)]),
        #     'true': forces[train[:len(test)]],
        # },
        # 'test': {
        #     'pred': evaluate(f, features, geometry, test[:len(train)]),
        #     'true': forces[test[:len(train)]],
        # },
        "encoder": encoder.state_dict() if args.save_state else None,
        "decoder": decoder.state_dict() if args.save_state else None,
    }


def main():
    results = execute(args)
    with open(args.pickle, "wb") as f:
        torch.save(results, f)


if __name__ == "__main__":
    main()
